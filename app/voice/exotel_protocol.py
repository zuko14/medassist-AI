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
        st = msg.get("start") if isinstance(msg.get("start"), dict) else {}
        fmt = st.get("media_format") or msg.get("media_format") or {}
        try:
            rate = int(fmt.get("sample_rate") or msg.get("sample_rate") or 8000)
        except (TypeError, ValueError):
            rate = 8000
        custom = st.get("custom_parameters") or msg.get("custom_parameters") or {}
        return StreamEvent(
            "start",
            stream_sid=st.get("stream_sid") or msg.get("stream_sid") or sid,
            call_sid=st.get("call_sid") or msg.get("call_sid"),
            from_number=st.get("from") or msg.get("from") or msg.get("From"),
            to_number=st.get("to") or msg.get("to") or msg.get("To"),
            custom=custom if isinstance(custom, dict) else {},
            sample_rate=rate,
        )
    if kind == "media":
        payload = (msg.get("media") or {}).get("payload") if isinstance(msg.get("media"), dict) else msg.get("payload") or ""
        try:
            audio = base64.b64decode(payload, validate=False)
        except (ValueError, TypeError) as e:
            raise ValueError(f"bad media payload: {e}") from e
        return StreamEvent("media", stream_sid=sid or (msg.get("media") or {}).get("stream_sid"), audio=audio)
    if kind == "dtmf":
        digit = str((msg.get("dtmf") or {}).get("digit") if isinstance(msg.get("dtmf"), dict) else msg.get("digit") or "")
        return StreamEvent("dtmf", stream_sid=sid, digit=digit)
    if kind == "mark":
        mark_name = (msg.get("mark") or {}).get("name") if isinstance(msg.get("mark"), dict) else msg.get("mark")
        return StreamEvent("mark", stream_sid=sid, mark=mark_name)
    if kind == "stop":
        st = msg.get("stop") if isinstance(msg.get("stop"), dict) else {}
        return StreamEvent(
            "stop",
            stream_sid=st.get("stream_sid") or sid,
            call_sid=st.get("call_sid") or msg.get("call_sid"),
            reason=st.get("reason") or msg.get("reason"),
        )
    raise ValueError(f"unknown event {kind!r}")


def media(stream_sid: str, pcm: bytes) -> str:
    return json.dumps({"event": "media", "stream_sid": stream_sid,
                       "media": {"payload": base64.b64encode(pcm).decode("ascii")}})


def mark(stream_sid: str, name: str) -> str:
    return json.dumps({"event": "mark", "stream_sid": stream_sid, "mark": {"name": name}})


def clear(stream_sid: str) -> str:
    """Drop audio Exotel has buffered but not yet played (barge-in)."""
    return json.dumps({"event": "clear", "stream_sid": stream_sid})
