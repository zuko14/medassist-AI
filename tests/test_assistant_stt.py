"""KK-P3-02: Kriya Ketu voice transcription (openrouter-and-speech-spec.md B.2 step 4, B.5).

POST /admin/assistant/transcribe takes the browser's MediaRecorder blob as the raw request body
(not multipart: Starlette spools multipart uploads over 1 MB to a temp file, and audio must never
touch disk). Covers VOICE-03 (failure mapping), VOICE-07 (size/mime/rate limits) and VOICE-08
(audio never stored, transcript never logged), plus the Sarvam REST request shape.
"""

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.assistant import stt
from app.config import settings
from app.main import app
from app.routers import admin
from app.routers import assistant as assistant_router
from app.routers.admin import AdminUser

CID = str(uuid.uuid4())
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 2000
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 2000
SECRET = "doctor ravi leave tomorrow"

client = TestClient(app)


@pytest.fixture
def env():
    """Ketu + STT on, a fake provider, spend cap open, ledger and limiter spied."""
    user = AdminUser(username="desk1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()),
                     staff_role="FRONT_DESK", permissions=[])
    app.dependency_overrides[admin.verify_credentials] = lambda: user
    clinic = {"id": CID, "plan": "polyclinic", "features": {"kriya_ketu": True}}
    fake = stt.FakeStt(text=SECRET, language="en-IN")
    ledger = MagicMock()
    with patch.object(settings, "assistant_enabled", True), \
         patch.object(settings, "assistant_stt_enabled", True), \
         patch.object(settings, "sarvam_api_key", "test-key"), \
         patch.object(settings, "voice_stt_paise_per_minute", 60), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=clinic)), \
         patch.object(assistant_router, "_stt_rate_limited", AsyncMock(return_value=False)) as limited, \
         patch.object(assistant_router, "check_admin_spend_cap", AsyncMock(return_value=(False, 0, 50000))) as cap, \
         patch.object(assistant_router, "record_ai_usage_bg", ledger), \
         patch.object(assistant_router, "STT_PROVIDER", fake):
        yield {"fake": fake, "ledger": ledger, "limited": limited, "cap": cap, "user": user}
    app.dependency_overrides.pop(admin.verify_credentials, None)


def _post(body=WEBM, mime="audio/webm;codecs=opus", **params):
    params.setdefault("duration_ms", 4200)
    return client.post("/admin/assistant/transcribe", content=body, params=params,
                       headers={"Content-Type": mime} if mime else {})


def _code(r):
    return r.json()["detail"]["error_code"]


# ── happy path and language mapping ────────────────────────────────────────────

def test_transcribes_and_returns_text_language_duration(env):
    r = _post()
    assert r.status_code == 200
    assert r.json() == {"text": SECRET, "language": "en-IN", "duration_ms": 4200}
    audio, mime, language_code, mode = env["fake"].calls[0]
    assert audio == WEBM and mime == "audio/webm"
    assert (language_code, mode) == ("unknown", "transcribe")


@pytest.mark.parametrize("hint,code,mode", [
    ("auto", "unknown", "transcribe"), ("te", "te-IN", "transcribe"), ("hi", "hi-IN", "transcribe"),
    ("en", "en-IN", "transcribe"), ("mixed", "unknown", "codemix"),
])
def test_language_hint_maps_to_sarvam_code_and_mode(env, hint, code, mode):
    assert _post(language=hint).status_code == 200
    assert env["fake"].calls[0][2:] == (code, mode)


def test_mp4_from_safari_is_accepted(env):
    assert _post(MP4, "audio/mp4").status_code == 200
    assert env["fake"].calls[0][1] == "audio/mp4"


def test_unknown_language_hint_is_422(env):
    assert _post(language="fr").status_code == 422
    assert env["fake"].calls == []


def test_transcript_is_trimmed_and_capped_to_the_composer_limit(env):
    env["fake"].text = "  " + "a" * 1500 + "  "
    assert len(_post().json()["text"]) == 1000


def test_empty_transcript_is_returned_as_empty_text(env):
    env["fake"].text = "   "
    r = _post()
    assert r.status_code == 200 and r.json()["text"] == ""   # the widget shows NO_SPEECH


# ── gates ──────────────────────────────────────────────────────────────────────

def test_stt_flag_off_is_403_and_provider_not_called(env):
    with patch.object(settings, "assistant_stt_enabled", False):
        r = _post()
    assert r.status_code == 403 and _code(r) == "stt_disabled"
    assert env["fake"].calls == []


def test_assistant_off_is_403(env):
    with patch.object(settings, "assistant_enabled", False):
        r = _post()
    assert r.status_code == 403 and _code(r) == "assistant_disabled"


def test_missing_sarvam_key_is_503(env):
    with patch.object(settings, "sarvam_api_key", ""):
        r = _post()
    assert r.status_code == 503 and _code(r) == "stt_unavailable"


def test_spend_cap_reached_is_503_before_the_provider(env):
    env["cap"].return_value = (True, 50000, 50000)
    r = _post()
    assert r.status_code == 503 and _code(r) == "stt_budget"
    assert env["fake"].calls == []
    env["cap"].assert_awaited_once_with(CID)


def test_super_admin_without_scope_is_400(env):
    root = AdminUser(username="root", role="super_admin", clinic_id=None, user_id=str(uuid.uuid4()),
                     staff_role=None, permissions=[])
    app.dependency_overrides[admin.verify_credentials] = lambda: root
    assert _post().status_code == 400


# ── VOICE-07: size, mime, rate ─────────────────────────────────────────────────

def test_over_one_megabyte_is_413_and_provider_not_called(env):
    r = _post(b"\x1a\x45\xdf\xa3" + b"\x00" * stt.MAX_BYTES)
    assert r.status_code == 413 and _code(r) == "audio_too_large"
    assert env["fake"].calls == []


def test_exactly_one_megabyte_is_accepted(env):
    assert _post(b"\x1a\x45\xdf\xa3" + b"\x00" * (stt.MAX_BYTES - 4)).status_code == 200


@pytest.mark.parametrize("mime", ["audio/wav", "video/webm", "application/octet-stream", "text/plain", None])
def test_wrong_content_type_is_415(env, mime):
    r = _post(mime=mime)
    assert r.status_code == 415 and _code(r) == "unsupported_audio"
    assert env["fake"].calls == []


def test_body_that_is_not_the_declared_format_is_415(env):
    r = _post(b"RIFF....WAVEfmt " + b"\x00" * 100, "audio/webm")
    assert r.status_code == 415
    assert _post(WEBM, "audio/mp4").status_code == 415
    assert env["fake"].calls == []


def test_empty_body_is_422(env):
    r = _post(b"")
    assert r.status_code == 422 and _code(r) == "empty_audio"


def test_rate_limited_is_429(env):
    env["limited"].return_value = True
    r = _post()
    assert r.status_code == 429 and _code(r) == "rate_limited"
    assert env["fake"].calls == []


def test_rate_limit_is_60_per_user_per_hour_keyed_by_clinic_and_user():
    assert assistant_router._stt_limiter.max_attempts == 60
    assert assistant_router._stt_limiter.window_seconds == 3600
    with patch.object(assistant_router._stt_limiter, "check_and_record", return_value=False) as rec:
        asyncio.run(assistant_router._stt_rate_limited(CID, "desk1"))
    rec.assert_called_once_with(f"assistant-stt:{CID}:desk1")


def test_duration_over_30_seconds_is_422(env):
    assert _post(duration_ms=30001).status_code == 422


# ── VOICE-03: failure mapping, fixed text ──────────────────────────────────────

@pytest.mark.parametrize("err,status,code", [("timeout", 504, "stt_timeout"), ("failed", 502, "stt_failed")])
def test_provider_failure_maps_to_fixed_codes(env, err, status, code):
    env["fake"].error = err
    r = _post()
    assert r.status_code == status and _code(r) == code
    assert r.json()["detail"]["message"] == "Voice service unavailable. Type your request."
    kwargs = env["ledger"].call_args.kwargs
    assert kwargs["success"] is False and kwargs["cost_paise"] == 0 and kwargs["error_message"] == err


def test_unexpected_provider_exception_is_502_without_its_text(env):
    env["fake"].error = RuntimeError("secret-internal-detail")
    r = _post()
    assert r.status_code == 502 and "secret-internal-detail" not in r.text


# ── cost ───────────────────────────────────────────────────────────────────────

def test_success_records_assistant_stt_cost_from_duration(env):
    _post(duration_ms=30000)   # 0.5 min x 60 paise/min
    kwargs = env["ledger"].call_args.kwargs
    assert kwargs["clinic_id"] == CID and kwargs["task_type"] == "assistant_stt"
    assert kwargs["provider"] == "sarvam" and kwargs["model"] == settings.assistant_stt_model
    assert kwargs["cost_paise"] == 30 and kwargs["success"] is True


def test_missing_duration_is_billed_at_the_30_second_cap(env):
    client.post("/admin/assistant/transcribe", content=WEBM, headers={"Content-Type": "audio/webm"})
    assert env["ledger"].call_args.kwargs["cost_paise"] == 30


def test_cost_rounds_up_to_a_whole_paisa():
    assert stt.cost_paise(1, 60) == 1
    assert stt.cost_paise(0, 60) == 0
    assert stt.cost_paise(60000, 0) == 0


# ── VOICE-08: nothing stored, transcript never logged ─────────────────────────

def test_audio_and_transcript_are_never_stored_or_logged(env, caplog):
    caplog.set_level(logging.DEBUG)
    with patch.object(assistant_router.store, "create_run") as create, \
         patch.object(assistant_router.store, "transition") as transition, \
         patch("app.database.sb") as sb, \
         patch("tempfile.SpooledTemporaryFile") as spooled, \
         patch("tempfile.NamedTemporaryFile") as named:
        r = _post()
    assert r.status_code == 200
    for spy in (create, transition, sb, spooled, named):
        spy.assert_not_called()
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "ASSISTANT_STT" in logged and SECRET not in logged
    for arg in list(env["ledger"].call_args.args) + list(env["ledger"].call_args.kwargs.values()):
        assert SECRET not in str(arg) and not isinstance(arg, (bytes, bytearray))


# ── Sarvam REST provider ───────────────────────────────────────────────────────

def _sarvam(handler):
    return stt.SarvamRest(transport=httpx.MockTransport(handler))


def test_sarvam_request_shape_and_parse():
    seen = {}

    def handler(req):
        seen["url"], seen["key"], seen["body"] = str(req.url), req.headers.get("api-subscription-key"), req.read()
        return httpx.Response(200, json={"request_id": "r1", "transcript": " నమస్తే ", "language_code": "te-IN"})

    with patch.object(settings, "sarvam_api_key", "k-123"):
        heard = asyncio.run(_sarvam(handler).transcribe(WEBM, "audio/webm", "te-IN", "transcribe"))
    assert heard == stt.Heard("నమస్తే", "te-IN")
    assert seen["url"] == "https://api.sarvam.ai/speech-to-text" and seen["key"] == "k-123"
    body = seen["body"]
    for part in (b'name="model"', settings.assistant_stt_model.encode(), b'name="language_code"', b"te-IN",
                 b'name="mode"', b"transcribe", b'name="file"', b"audio/webm", WEBM[:4]):
        assert part in body


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": "bad"}), httpx.Response(500, text="boom"),
    httpx.Response(200, text="not json"), httpx.Response(200, json={"no_transcript": 1}),
])
def test_sarvam_bad_responses_are_failed(response):
    with pytest.raises(stt.SttError) as e:
        asyncio.run(_sarvam(lambda req: response).transcribe(WEBM, "audio/webm", "unknown", "transcribe"))
    assert e.value.code == "failed"


def test_sarvam_network_error_is_failed_and_timeout_is_timeout():
    def boom(req):
        raise httpx.ConnectError("down")

    def slow(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(stt.SttError) as e:
        asyncio.run(_sarvam(boom).transcribe(WEBM, "audio/webm", "unknown", "transcribe"))
    assert e.value.code == "failed"
    with pytest.raises(stt.SttError) as e:
        asyncio.run(_sarvam(slow).transcribe(WEBM, "audio/webm", "unknown", "transcribe"))
    assert e.value.code == "timeout"


def test_sarvam_total_time_is_capped_by_the_setting():
    async def hang(req):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"transcript": "late"})

    with patch.object(settings, "assistant_stt_timeout_seconds", 0.05), pytest.raises(stt.SttError) as e:
        asyncio.run(_sarvam(hang).transcribe(WEBM, "audio/webm", "unknown", "transcribe"))
    assert e.value.code == "timeout"
