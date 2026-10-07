"""Gateway: a whole call over a fake Exotel socket (marks echoed like Exotel
does after playback), fake STT/TTS, and the real session + dialog."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.voice import gateway
from app.voice.providers.fake import FakeSTT, FakeTTS
from app.voice.session import CallSession

from tests.voice.test_dialog import DOCS, SLOTS, FakeTools
from tests.voice.test_io_layer import CLINIC, NOW

START = {"event": "start", "stream_sid": "S1",
         "start": {"stream_sid": "S1", "call_sid": "CA1", "from": "09876543210", "to": "+914071234567",
                   "media_format": {"encoding": "raw", "sample_rate": "8000"}}}
CALL = {"id": "call-1", "call_ref": "CALL-1", "clinic_id": CLINIC["id"], "caller_phone": "+919876543210",
        "direction": "inbound", "language": "te-IN"}


class FakeWS:
    def __init__(self, echo_marks=True):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.echo = echo_marks

    async def accept(self):
        pass

    async def receive_text(self):
        return await self.inbox.get()

    async def send_text(self, msg):
        self.sent.append(json.loads(msg))
        m = self.sent[-1]
        if m["event"] == "mark" and self.echo:
            self.inbox.put_nowait(json.dumps({"event": "mark", "stream_sid": "S1", "mark": m["mark"]}))

    async def close(self, code=1000):
        self.closed = True

    def kinds(self):
        return [m["event"] for m in self.sent]


def _patches(tools, finish):
    def make_session(ctx, dialog_state=None):
        s = CallSession(ctx, tools=tools, now=NOW)
        s.finish = finish
        return s
    stt, tts = FakeSTT(), FakeTTS()
    return stt, [
        patch.object(gateway.settings, "voice_enabled", True),
        patch.object(gateway.settings, "voice_stream_token", "tok"),
        patch("app.voice.gateway.admit", new=AsyncMock(return_value=(CALL, CLINIC, {"reception_number": "+9140"}, None))),
        patch("app.voice.gateway.CallSession", new=make_session),
        patch("app.voice.gateway.get_providers", new=lambda: (stt, tts)),
        patch("app.voice.session.store.update_call", new=AsyncMock()),
        patch("app.voice.session.store.add_event", new=AsyncMock()),
        patch("app.voice.gateway.store.add_event", new=AsyncMock()),
        patch("app.voice.session.store.get_call", new=AsyncMock(return_value={})),
        patch("app.voice.session.store.load_lexicon", new=AsyncMock(return_value=[])),
        patch("app.database.get_doctors", new=AsyncMock(return_value=DOCS)),
    ]


async def _until(cond, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_full_call_books_and_hangs_up_cleanly():
    tools = FakeTools(slots=SLOTS[:1])
    finish = AsyncMock()
    stt, ps = _patches(tools, finish)
    ws = FakeWS()
    for p in ps:
        p.start()
    try:
        ws.inbox.put_nowait(json.dumps({"event": "connected"}))
        ws.inbox.put_nowait(json.dumps(START))
        task = asyncio.create_task(gateway.exotel_stream(ws, k="tok"))
        await _until(lambda: stt.streams and ws.kinds().count("mark") >= 1)
        stream = stt.streams[0]
        stream.say("Naaku repu heart doctor appointment kavali morning")
        await _until(lambda: ws.kinds().count("mark") >= 2)
        stream.say("avunu")
        await _until(lambda: ws.kinds().count("mark") >= 3)
        stream.say("vaddu, thanks")
        await asyncio.wait_for(task, 5)
    finally:
        for p in ps:
            p.stop()
    assert ws.closed and "media" in ws.kinds()
    assert [c for c in tools.calls if c[0] == "book"]
    assert finish.await_args.args[0] == "completed"


@pytest.mark.asyncio
async def test_barge_in_clears_exotel_buffer():
    finish = AsyncMock()
    stt, ps = _patches(FakeTools(slots=SLOTS), finish)
    ws = FakeWS(echo_marks=False)           # greeting never finishes playing
    for p in ps:
        p.start()
    try:
        ws.inbox.put_nowait(json.dumps(START))
        task = asyncio.create_task(gateway.exotel_stream(ws, k="tok"))
        await _until(lambda: stt.streams and "mark" in ws.kinds())
        stt.streams[0].queue.put_nowait(("speech_start", None))
        await _until(lambda: "clear" in ws.kinds())
        ws.inbox.put_nowait(json.dumps({"event": "stop", "stream_sid": "S1",
                                        "stop": {"call_sid": "CA1", "reason": "callended"}}))
        await asyncio.wait_for(task, 5)
    finally:
        for p in ps:
            p.stop()
    assert finish.await_args.args[0] == "dropped"


@pytest.mark.asyncio
async def test_refused_call_closes_stream_without_a_session():
    ws = FakeWS()
    ws.inbox.put_nowait(json.dumps(START))
    with patch.object(gateway.settings, "voice_enabled", True), \
         patch.object(gateway.settings, "voice_stream_token", "tok"), \
         patch("app.voice.gateway.admit", new=AsyncMock(return_value=(None, None, None, "feature_disabled"))), \
         patch("app.voice.gateway.CallSession") as cs:
        await gateway.exotel_stream(ws, k="tok")
    assert ws.closed and not ws.sent
    cs.assert_not_called()


@pytest.mark.asyncio
async def test_bad_token_is_rejected_before_accept():
    ws = FakeWS()
    ws.accept = AsyncMock()
    with patch.object(gateway.settings, "voice_enabled", True), patch.object(gateway.settings, "voice_stream_token", "tok"):
        await gateway.exotel_stream(ws, k="wrong")
    ws.accept.assert_not_awaited()
    assert ws.closed


def _start_with_custom(custom):
    return json.dumps({**START, "start": {**START["start"], "custom_parameters": custom}})


@pytest.mark.asyncio
async def test_token_in_start_custom_parameters_is_admitted():
    """Exotel can strip ?k= from the handshake and deliver it in start.custom_parameters."""
    ws = FakeWS()
    ws.inbox.put_nowait(_start_with_custom({"k": "tok"}))
    admit = AsyncMock(return_value=(None, None, None, "feature_disabled"))
    with patch.object(gateway.settings, "voice_enabled", True), \
         patch.object(gateway.settings, "voice_stream_token", "tok"), \
         patch("app.voice.gateway.admit", new=admit):
        await gateway.exotel_stream(ws, k="")
    admit.assert_awaited_once()


@pytest.mark.parametrize("custom", [{}, {"k": "wrong"}])
@pytest.mark.asyncio
async def test_missing_token_everywhere_is_rejected_before_admission(custom):
    ws = FakeWS()
    ws.inbox.put_nowait(_start_with_custom(custom))
    admit = AsyncMock()
    with patch.object(gateway.settings, "voice_enabled", True), \
         patch.object(gateway.settings, "voice_stream_token", "tok"), \
         patch("app.voice.gateway.admit", new=admit):
        await gateway.exotel_stream(ws, k="")
    admit.assert_not_awaited()
    assert ws.closed and not ws.sent


@pytest.mark.parametrize("row, code", [
    ({"status": "completed", "handoff_reason": None}, 302),
    ({"status": "handed_off", "handoff_reason": "caller_requested_human"}, 200),
    ({"status": "in_progress", "handoff_reason": None}, 200),     # crashed mid-call -> human
    (None, 200),                                                  # we never saw it -> human
])
def test_passthru_always_fails_safe_to_reception(row, code):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(gateway.router)
    with patch.object(gateway.settings, "voice_stream_token", "tok"), \
         patch("app.voice.gateway.store.get_call_by_sid", new=AsyncMock(return_value=row)):
        r = TestClient(app).get("/voice/exotel/passthru?k=tok&CallSid=CA1", follow_redirects=False)
    assert r.status_code == code


async def _run_until_stopped(ws, stt, ps, script):
    for p in ps:
        p.start()
    try:
        ws.inbox.put_nowait(json.dumps(START))
        task = asyncio.create_task(gateway.exotel_stream(ws, k="tok"))
        await _until(lambda: stt.streams and "mark" in ws.kinds())
        await script(stt.streams[0])
        ws.inbox.put_nowait(json.dumps({"event": "stop", "stream_sid": "S1",
                                        "stop": {"call_sid": "CA1", "reason": "callended"}}))
        await asyncio.wait_for(task, 5)
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_hello_over_the_greeting_replays_it_instead_of_a_turn():
    """Production: callers say "hello?" as the line connects; the greeting was cut and the
    "hello" counted as a misunderstanding. Now the greeting is said again in full."""
    tools = FakeTools(slots=SLOTS)
    stt, ps = _patches(tools, AsyncMock())
    handle = AsyncMock()
    ps.append(patch.object(CallSession, "handle", new=handle))
    ws = FakeWS(echo_marks=False)

    async def script(stream):
        stream.say("హలో.")
        await _until(lambda: ws.kinds().count("mark") >= 2)

    await _run_until_stopped(ws, stt, ps, script)
    assert "clear" in ws.kinds()                 # stopped talking over the caller...
    assert ws.kinds().count("mark") >= 2         # ...then said the greeting again
    handle.assert_not_awaited()                  # "hello" never reached the dialog


@pytest.mark.asyncio
async def test_real_speech_over_kriya_still_interrupts_and_is_answered():
    tools = FakeTools(slots=SLOTS)
    stt, ps = _patches(tools, AsyncMock())
    ws = FakeWS(echo_marks=False)
    seen = []
    real_handle = CallSession.handle

    async def spy(self, text, lang=None):
        seen.append(text)
        return await real_handle(self, text, lang)
    ps.append(patch.object(CallSession, "handle", new=spy))

    async def script(stream):
        stream.say("Naaku repu heart doctor appointment kavali")
        await _until(lambda: seen)

    await _run_until_stopped(ws, stt, ps, script)
    assert "clear" in ws.kinds() and seen == ["Naaku repu heart doctor appointment kavali"]
