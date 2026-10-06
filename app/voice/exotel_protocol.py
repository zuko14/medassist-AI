"""Exotel Voicebot applet (bidirectional stream) wire protocol.

Source of truth: developer.exotel.com/docs/agentstream/stream-voicebot-applet
(checked 2026-10-06). Inbound events: connected, start, media, dtmf, mark,
stop. Outbound messages: media, mark, clear. Audio is base64 of 16-bit LE mono
PCM; 8000 Hz unless the stream URL asks otherwise.
"""

import base64
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StreamEvent:
    kind: str                                  # connected|start|media|dtmf|mark|stop
    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    custom: dict = field(default_factory=dict)
    sample_rate: int = 8000
    audio: bytes = b""
    digit: Optional[str] = None
    mark: Optional[str] = None
    reason: Optional[str] = None


def parse(raw) -> StreamEvent:
    """Decode one frame. Raises ValueError on anything that is not a known event."""
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"not JSON: {e}") from e
    if not isinstance(msg, dict):
        raise ValueError("frame is not an object")
    kind = msg.get("event")
    sid = msg.get("stream_sid")
    if kind == "connected":
        return StreamEvent("connected")
    if kind == "start":
        st = msg.get("start") or {}
        fmt = st.get("media_format") or {}
        try:
            rate = int(fmt.get("sample_rate") or 8000)
        except (TypeError, ValueError):
            rate = 8000
        custom = st.get("custom_parameters") or {}
        return StreamEvent("start", stream_sid=st.get("stream_sid") or sid, call_sid=st.get("call_sid"),
                           from_number=st.get("from"), to_number=st.get("to"),
                           custom=custom if isinstance(custom, dict) else {}, sample_rate=rate)
    if kind == "media":
        payload = (msg.get("media") or {}).get("payload") or ""
        try:
            audio = base64.b64decode(payload, validate=False)
        except (ValueError, TypeError) as e:
            raise ValueError(f"bad media payload: {e}") from e
        return StreamEvent("media", stream_sid=sid, audio=audio)
    if kind == "dtmf":
        return StreamEvent("dtmf", stream_sid=sid, digit=str((msg.get("dtmf") or {}).get("digit") or ""))
    if kind == "mark":
        return StreamEvent("mark", stream_sid=sid, mark=(msg.get("mark") or {}).get("name"))
    if kind == "stop":
        st = msg.get("stop") or {}
        return StreamEvent("stop", stream_sid=sid, call_sid=st.get("call_sid"), reason=st.get("reason"))
    raise ValueError(f"unknown event {kind!r}")


def media(stream_sid: str, pcm: bytes) -> str:
    return json.dumps({"event": "media", "stream_sid": stream_sid,
                       "media": {"payload": base64.b64encode(pcm).decode("ascii")}})


def mark(stream_sid: str, name: str) -> str:
    return json.dumps({"event": "mark", "stream_sid": stream_sid, "mark": {"name": name}})


def clear(stream_sid: str) -> str:
    """Drop audio Exotel has buffered but not yet played (barge-in)."""
    return json.dumps({"event": "clear", "stream_sid": stream_sid})
