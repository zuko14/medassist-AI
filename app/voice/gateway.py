"""Telephony gateway: Exotel Voicebot stream <-> STT <-> CallSession <-> TTS.

Exotel flow per Exophone (configured in the Exotel dashboard, see
docs/voice/telephony-setup.md):
    [Voicebot applet  wss://<host>/voice/exotel/stream?k=<VOICE_STREAM_TOKEN>]
        -> [Passthru applet  https://<host>/voice/exotel/passthru?k=<token>]
              200 -> [Connect applet: hospital reception number]
              302 -> [Hangup]
When the bot closes the stream, Exotel continues to Passthru. Kriya answers 200
(= give the caller a human) whenever the AI did not finish the call cleanly:
transfer requested, emergency, refused admission, crash, or no record at all.
A Kriya outage therefore degrades to "caller reaches reception", never to silence.
"""

import asyncio
import hmac
import logging
import time
from typing import Optional

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from app.config import settings
from app.services.tenant import TenantNotFound, ai_receptionist_enabled, get_clinic_by_id

from . import exotel_protocol as X
from . import store
from .audio import PcmChunker, seconds_of
from .phone import mask, to_e164
from .providers import get_providers
from .session import CallSession
from .tools import CallContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["voice"])

_ACTIVE: dict = {}          # call_id -> CallRunner, this process only
END_STATES = ("completed", "dropped", "no_answer")


def token_ok(k: Optional[str]) -> bool:
    return bool(settings.voice_stream_token) and hmac.compare_digest(k or "", settings.voice_stream_token)


class CallRunner:
    """Drives one live call. Speech runs in its own task so barge-in events keep flowing."""

    def __init__(self, ws, stream_sid: str, session: CallSession, stt_stream, tts, sample_rate: int = 8000):
        self.ws, self.sid, self.session = ws, stream_sid, session
        self.stt, self.tts, self.rate = stt_stream, tts, sample_rate
        self.cfg = session.cfg
        self.speak_task: Optional[asyncio.Task] = None
        self.pending_mark: Optional[str] = None
        self.mark_done = asyncio.Event()
        self.marks = 0
        self.stt_bytes = 0
        self.tts_chars = 0
        self.turn_ms: list = []
        self.started = time.monotonic()
        self.last_activity = time.monotonic()
        self.silences = 0
        self.control = "continue"
        self.finished = asyncio.Event()
        self.caller_hung_up = False

    @property
    def speaking(self) -> bool:
        return bool(self.speak_task and not self.speak_task.done())

    async def _send(self, msg: str) -> None:
        try:
            await self.ws.send_text(msg)
        except Exception:
            self.caller_hung_up = True
            self.finished.set()

    async def _speak(self, texts: list, heard_at: Optional[float]) -> None:
        first = True
        for text in texts:
            self.tts_chars += len(text)
            chunker = PcmChunker()
            async for pcm in self.tts.synthesize(text, self.session.state["lang"], self.cfg.get("speaker"),
                                                 self.cfg.get("pace") or 1.0, self.rate):
                for c in chunker.feed(pcm):
                    if first and heard_at is not None:
                        self.turn_ms.append(int((time.monotonic() - heard_at) * 1000))
                        first = False
                    await self._send(X.media(self.sid, c))
            for c in chunker.flush():
                await self._send(X.media(self.sid, c))
        self.marks += 1
        self.pending_mark = f"u{self.marks}"
        self.mark_done.clear()
        await self._send(X.mark(self.sid, self.pending_mark))
        try:
            await asyncio.wait_for(self.mark_done.wait(), timeout=20)
        except asyncio.TimeoutError:
            pass
        self.pending_mark = None
        self.last_activity = time.monotonic()
        if self.control != "continue":
            self.finished.set()

    async def respond(self, reply, heard_at: Optional[float] = None) -> None:
        await self.stop_speaking(clear=False)
        self.control = reply.control
        self.speak_task = asyncio.create_task(self._speak(reply.texts, heard_at))

    async def stop_speaking(self, clear: bool = True) -> None:
        if self.speaking:
            self.speak_task.cancel()
            try:
                await self.speak_task
            except (asyncio.CancelledError, Exception):
                pass
            if clear:
                await self._send(X.clear(self.sid))

    async def pump_audio(self) -> None:
        try:
            while not self.finished.is_set():
                ev = X.parse(await self.ws.receive_text())
                if ev.kind == "media":
                    self.stt_bytes += len(ev.audio)
                    await self.stt.send(ev.audio)
                elif ev.kind == "mark" and ev.mark == self.pending_mark:
                    self.mark_done.set()
                elif ev.kind == "stop":
                    self.caller_hung_up = ev.reason == "callended"
                    break
        except (WebSocketDisconnect, RuntimeError):
            self.caller_hung_up = True
        except ValueError as e:
            logger.warning(f"VOICE_BAD_FRAME call={self.session.ctx.call_ref}: {e}")
        finally:
            self.finished.set()

    async def pump_stt(self) -> None:
        async for kind, payload in self.stt.events():
            if self.finished.is_set():
                return
            if kind == "speech_start" and self.speaking and self.control == "continue":
                await self.stop_speaking(clear=True)          # barge-in
                await store.add_event(self.session.ctx.clinic_id, self.session.ctx.call_id, "system",
                                      "BARGE_IN", status="ok")
            elif kind == "transcript":
                text, lang = payload
                heard = time.monotonic()
                self.last_activity, self.silences = heard, 0
                if self.control != "continue":
                    continue
                reply = await self.session.handle(text, lang)
                await self.respond(reply, heard)
            elif kind == "error":
                await self.respond(await self.session.silence(2))  # polite close; passthru transfers
                self.control = "transfer"

    async def watchdog(self) -> None:
        limit = settings.voice_silence_reprompt_seconds
        while not self.finished.is_set():
            await asyncio.sleep(1)
            if time.monotonic() - self.started > settings.voice_max_call_seconds and self.control == "continue":
                await store.add_event(self.session.ctx.clinic_id, self.session.ctx.call_id, "system",
                                      "MAX_DURATION", status="ok")
                out = await self.session.engine._handoff(self.session.state, self.session.engine_out(),
                                                         "max_call_duration")
                await self.respond(await self.session._emit(out))
            elif (not self.speaking and self.control == "continue"
                  and time.monotonic() - self.last_activity > limit):
                self.silences += 1
                self.last_activity = time.monotonic()
                await self.respond(await self.session.silence(self.silences))

    async def run(self, greeting) -> str:
        await self.respond(greeting)
        tasks = [asyncio.create_task(self.pump_audio()), asyncio.create_task(self.pump_stt()),
                 asyncio.create_task(self.watchdog())]
        try:
            await self.finished.wait()
        finally:
            for t in tasks + ([self.speak_task] if self.speak_task else []):
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stt.close()
        if self.control == "transfer":
            return "handed_off"
        if self.control == "end":
            return "completed"
        return "dropped" if self.caller_hung_up else "completed"

    def usage(self) -> dict:
        return {"telephony_seconds": int(time.monotonic() - self.started),
                "stt_seconds": seconds_of(self.stt_bytes), "tts_chars": self.tts_chars,
                "avg_turn_ms": int(sum(self.turn_ms) / len(self.turn_ms)) if self.turn_ms else None}


async def _wait_start(ws) -> X.StreamEvent:
    while True:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        ev = X.parse(raw)
        logger.info(f"VOICE_STREAM_EVENT kind={ev.kind}")
        if ev.kind == "start":
            return ev


async def admit(ev: X.StreamEvent):
    """(call_row, clinic, number_row, refusal_reason). Refusal => the caller goes to reception."""
    caller = to_e164(ev.from_number) or (ev.from_number or "unknown")
    exophone = to_e164(ev.to_number) or ev.to_number
    existing = None
    for _ in range(3):  # an outbound row is written just after Exotel accepts the dial
        existing = await store.get_call_by_sid(ev.call_sid)
        if existing:
            break
        await asyncio.sleep(0.3)
    number = await store.resolve_number(existing.get("exophone") if existing else exophone)
    clinic_id = existing["clinic_id"] if existing else (number or {}).get("clinic_id")
    if not clinic_id:
        logger.warning(f"VOICE_UNKNOWN_NUMBER exophone={exophone} raw_to={ev.to_number} from={mask(caller)}")
        return None, None, None, "unknown_number"
    try:
        clinic = await get_clinic_by_id(clinic_id)
    except TenantNotFound:
        return None, None, None, "clinic_not_found"
    reason = None
    if not clinic.get("is_active", True) or str(clinic.get("id")) != str(clinic_id):
        reason = "clinic_inactive"
    elif not ai_receptionist_enabled(clinic):
        reason = "feature_disabled"
    elif store.voice_config(clinic).get("mode") == "shadow":
        reason = "shadow_mode"
    elif len(_ACTIVE) >= settings.voice_max_concurrent_calls:
        reason = "capacity"
    elif await store.budget_exceeded(clinic):
        reason = "budget_exhausted"
    fields = {"provider_call_sid": ev.call_sid, "stream_sid": ev.stream_sid, "exophone": exophone,
              "status": "handed_off" if reason else "in_progress"}
    if reason:
        fields.update({"handoff_reason": f"not_answered:{reason}", "outcome": "not_answered"})
    if existing:
        await store.update_call(clinic_id, existing["id"], fields)
        call = {**existing, **fields}
    else:
        call = await store.create_call(clinic_id, {**fields, "direction": "inbound", "caller_phone": caller,
                                                    "branch_id": (number or {}).get("branch_id"),
                                                    "language": store.voice_config(clinic)["primary_language"]})
        if not call:
            return None, clinic, number, "db_unavailable"
    return call, clinic, number, reason


@router.websocket("/voice/exotel/stream")
async def exotel_stream(ws: WebSocket, k: str = ""):
    if not settings.voice_enabled or not token_ok(k):
        logger.warning(f"VOICE_STREAM_REJECTED enabled={settings.voice_enabled} token_ok={token_ok(k)}")
        await ws.close(code=1008)
        return
    await ws.accept()
    logger.info("VOICE_STREAM_CONNECTED")
    try:
        ev = await _wait_start(ws)
    except Exception as e:
        logger.error(f"VOICE_STREAM_WAIT_START_FAILED: {type(e).__name__}: {e}")
        await ws.close()
        return
    logger.info(f"VOICE_STREAM_START call_sid={ev.call_sid} to={ev.to_number} from={mask(ev.from_number)}")
    call, clinic, number, refusal = await admit(ev)
    if refusal:
        logger.info(f"VOICE_NOT_ANSWERED reason={refusal} to={ev.to_number} from={mask(ev.from_number)}")
        await ws.close()
        return
    ctx = CallContext(call_id=call["id"], call_ref=call["call_ref"], clinic=clinic,
                      branch_id=call.get("branch_id"), caller_phone=call["caller_phone"],
                      mode=call.get("mode") or "live", reception_number=(number or {}).get("reception_number"),
                      lang=call.get("language") or "te-IN", correlation_id=call["call_ref"])
    session = CallSession(ctx, dialog_state=call.get("dialog") or None)
    stt, tts = get_providers()
    runner = None
    status = "failed"
    _ACTIVE[call["id"]] = True
    try:
        stt_stream = await stt.open(ctx.lang, ev.sample_rate)
        runner = CallRunner(ws, ev.stream_sid, session, stt_stream, tts, ev.sample_rate)
        _ACTIVE[call["id"]] = runner
        outbound = call.get("direction") == "outbound"
        interest = ((call.get("handoff_packet") or {}).get("interest") if outbound else None)
        status = await runner.run(await session.start(outbound=outbound, interest=interest))
    except Exception as e:
        logger.error(f"VOICE_CALL_CRASHED call={call['call_ref']}: {type(e).__name__}: {e}")
        await store.add_event(ctx.clinic_id, ctx.call_id, "error", "CALL_CRASHED", status="fail",
                              data={"error": type(e).__name__})
        status = "failed"
    finally:
        _ACTIVE.pop(call["id"], None)
        usage = runner.usage() if runner else {}
        await session.finish(status, **usage)
        if call.get("direction") == "outbound":
            from .outbound import job_finished
            await job_finished(call, success=status in ("completed", "handed_off"))
        try:
            await ws.close()
        except Exception:
            pass


@router.get("/voice/exotel/passthru")
async def exotel_passthru(request: Request, k: str = ""):
    """200 = connect the caller to reception; 302 = hang up. Unknown or unfinished => 200."""
    if not token_ok(k):
        return Response(status_code=200)  # never strand a caller, even on misconfiguration
    sid = request.query_params.get("CallSid") or request.query_params.get("call_sid")
    call = await store.get_call_by_sid(sid)
    if call and call.get("status") == "completed" and not call.get("handoff_reason"):
        return Response(status_code=302)
    return Response(status_code=200)


@router.post("/voice/exotel/status")
async def exotel_status(request: Request, k: str = ""):
    """Outbound call status callback (no-answer / busy / failed before the stream started)."""
    if not token_ok(k):
        return Response(status_code=403)
    form = await request.form()
    sid, st = form.get("CallSid"), (form.get("Status") or "").lower()
    call = await store.get_call_by_sid(sid)
    if call and call.get("status") in ("queued", "ringing") and st in ("no-answer", "busy", "failed", "canceled"):
        await store.update_call(call["clinic_id"], call["id"], {"status": "no_answer" if st != "failed" else "failed",
                                                                "outcome": st})
        from .outbound import job_finished
        await job_finished(call, success=False, error=st)
    return Response(status_code=200)
