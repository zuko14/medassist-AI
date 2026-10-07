"""I/O layer of the voice receptionist: safety screen, LLM fallback validation,
tool ownership + read-back verification, and the session pipeline."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.voice import nlu_llm, safety, store
from app.voice.dates import IST
from app.voice.nlu_rules import NluContext
from app.voice.session import CallSession
from app.voice.tools import CallContext, KriyaTools

from tests.voice.test_dialog import DOCS, SLOTS, FakeTools

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=IST)
CLINIC = {"id": "11111111-1111-1111-1111-111111111111", "name": "ABC Hospitals", "plan": "polyclinic",
          "account_type": "tenant", "features": {"ai_receptionist": True},
          "config": {"emergency_number": "040-1234", "voice": {"primary_language": "te-IN"}}}


def ctx(**kw):
    base = dict(call_id="call-1", call_ref="CALL-1", clinic=CLINIC, branch_id=None,
                caller_phone="+919876543210", reception_number="+914012345678")
    base.update(kw)
    return CallContext(**base)


# ---- safety ----

@pytest.mark.parametrize("text, verdict", [
    ("నాకు ఛాతీలో బాగా నొప్పిగా ఉంది", "emergency"),
    ("chest lo pain ga undi", "emergency"),
    ("मुझे सीने में तेज़ दर्द है", "emergency"),
    ("oopiri aadatledu", "emergency"),
    ("my father is unconscious", "emergency"),
    ("what tablet should I take for fever", "clinical"),
    ("repu cardiology appointment kavali", None),
])
def test_safety_screen(text, verdict):
    assert safety.screen(text, "te-IN") == verdict


# ---- LLM fallback ----

def test_llm_output_is_validated_not_trusted():
    c = NluContext(departments=["Cardiology"], now=NOW)
    r = nlu_llm._validate({"intents": ["BOOK_APPOINTMENT", "DROP_TABLES", "AFFIRM"], "specialty": "CARDIOLOGY",
                           "date": "2020-01-01", "time_period": "MIDNIGHT", "relation": "BOSS",
                           "confidence": 0.99}, c)
    assert r.intents == ["BOOK_APPOINTMENT", "AFFIRM"]
    assert r.entities == {"specialty": "CARDIOLOGY", "department": "Cardiology"}
    assert r.confidence == 0.9 and r.source == "llm"


def test_llm_cannot_raise_emergencies_or_turn_greetings_into_transfers():
    c = NluContext(now=NOW)
    for raw in ({"intents": ["GREETING"], "confidence": 0.8}, {"intents": ["EMERGENCY"], "confidence": 0.9},
                {"intents": ["CLINICAL_QUERY"], "confidence": 0.9}):
        assert nlu_llm._validate(raw, c).intents == []
    r = nlu_llm._validate({"intents": ["SERVICE_AVAILABILITY", "DEPARTMENT_INFORMATION"], "confidence": 0.8}, c)
    assert r.intents == ["DOCTOR_AVAILABILITY"]


def test_llm_can_never_consent_to_a_transaction():
    """While Kriya waits for 'shall I book/cancel?', only a yes the rules heard counts."""
    r = nlu_llm._validate({"intents": ["AFFIRM"], "confidence": 0.9}, NluContext(now=NOW), expect="confirm")
    assert r.intents == [] and r.confidence == 0.0


@pytest.mark.asyncio
async def test_llm_uses_live_backup_model_and_question_context():
    gw = AsyncMock(return_value={"choices": [{"message": {"content": '{"intents": ["GENERAL_INFORMATION"], "confidence": 0.8}'}}],
                                 "usage": {"total_tokens": 50}})
    with patch("app.voice.nlu_llm.call_ai_gateway", new=gw):
        r, tokens, _ = await nlu_llm.understand_llm("vere samacharam kavali", NluContext(now=NOW), CLINIC["id"],
                                                   expect="anything_else")
    assert r.intents == ["GENERAL_INFORMATION"] and tokens == 50
    kw = gw.await_args.kwargs
    assert kw["fallback_model"] == nlu_llm.settings.voice_llm_fallback_model
    assert "whether they need anything else" in gw.await_args.args[0][1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", ["bad_json", "timeout"])
async def test_llm_failure_means_not_understood(behaviour):
    if behaviour == "bad_json":
        gw = AsyncMock(return_value={"choices": [{"message": {"content": "not json"}}], "usage": {}})
    else:
        gw = AsyncMock(side_effect=TimeoutError("slow"))
    with patch("app.voice.nlu_llm.call_ai_gateway", new=gw):
        r, tokens, cost = await nlu_llm.understand_llm("something odd", NluContext(now=NOW), CLINIC["id"])
    assert r.confidence == 0.0 and r.intents == [] and tokens == 0
    assert gw.await_args.kwargs["max_attempts"] == 1 and gw.await_args.kwargs["task_type"] == "voice_nlu"


# ---- tools ----

def _sb_returning(*datas):
    it = iter(datas)
    return AsyncMock(side_effect=lambda q: MagicMock(data=next(it)))


@pytest.mark.asyncio
async def test_cancel_refuses_a_booking_on_another_number():
    t = KriyaTools(ctx())
    with patch("app.voice.tools.sb", new=_sb_returning([{"id": "a1", "status": "confirmed",
                                                         "patient_phone": "+919000000000"}])), \
         patch("app.voice.tools.store.add_event", new=AsyncMock()), \
         patch("app.services.conversation.conversation_manager._cancel_with_refund", new=AsyncMock()) as c:
        res = await t.cancel({"id": "a1"})
    assert res == {"status": "failed", "verified": False}
    c.assert_not_awaited()


@pytest.mark.asyncio
async def test_paid_booking_is_verified_by_read_back_and_link_sent():
    t = KriyaTools(ctx())
    created = {"success": True, "booking_id": "b1", "booking_ref": "KR-1", "payment_link": "https://rzp.io/x",
               "amount_paise": 80000}
    good_row = {"id": "b1", "status": "pending_payment", "booking_ref": "KR-1", "doctor_id": "d1",
                "appointment_date": "2026-10-07", "appointment_time": "10:30:00", "amount_paise": 80000}
    opt = {"doctor_id": "d1", "doctor_name": "Dr. Srinivas Rao", "department": "Cardiology",
           "date": "2026-10-07", "time": "10:30"}
    with patch("app.services.payment.resolve_payment_mode", return_value=("full", 100)), \
         patch("app.services.payment.payment_service.create_booking_with_payment",
               new=AsyncMock(return_value=created)) as create, \
         patch("app.voice.tools.sb", new=_sb_returning([good_row])), \
         patch.object(KriyaTools, "caller_profile", new=AsyncMock(return_value={"id": "p1", "name": "Ravi"})), \
         patch.object(KriyaTools, "_send_payment_link", new=AsyncMock(return_value=True)), \
         patch("app.voice.tools.store.add_event", new=AsyncMock()):
        res = await t.book(opt, "Ravi", "SELF")
    assert res["status"] == "payment_pending" and res["verified"] is True and res["link_sent"] is True
    kw = create.await_args.kwargs
    assert kw["clinic_id"] == CLINIC["id"] and kw["patient_phone"] == "+919876543210" and kw["doctor_id"] == "d1"


@pytest.mark.asyncio
async def test_read_back_mismatch_is_not_verified_and_no_link_sent():
    t = KriyaTools(ctx())
    created = {"success": True, "booking_id": "b1", "booking_ref": "KR-1", "payment_link": "https://rzp.io/x"}
    wrong = {"id": "b1", "status": "pending_payment", "doctor_id": "OTHER", "appointment_date": "2026-10-07",
             "appointment_time": "10:30:00"}
    opt = {"doctor_id": "d1", "doctor_name": "Dr. Rao", "department": "Cardiology", "date": "2026-10-07",
           "time": "10:30"}
    with patch("app.services.payment.resolve_payment_mode", return_value=("full", 100)), \
         patch("app.services.payment.payment_service.create_booking_with_payment",
               new=AsyncMock(return_value=created)), \
         patch("app.voice.tools.sb", new=_sb_returning([wrong])), \
         patch.object(KriyaTools, "caller_profile", new=AsyncMock(return_value=None)), \
         patch.object(KriyaTools, "_send_payment_link", new=AsyncMock(return_value=True)) as send, \
         patch("app.voice.tools.store.add_event", new=AsyncMock()):
        res = await t.book(opt, "Ravi", "SELF")
    assert res["verified"] is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_conflict_maps_to_slot_taken():
    t = KriyaTools(ctx())
    with patch("app.services.payment.resolve_payment_mode", return_value=("none", 100)), \
         patch("app.database.book_appointment", new=AsyncMock(return_value={"success": False, "reason": "slot_taken"})), \
         patch.object(KriyaTools, "caller_profile", new=AsyncMock(return_value=None)), \
         patch("app.voice.tools.store.add_event", new=AsyncMock()):
        res = await t.book({"doctor_id": "d1", "doctor_name": "Dr. Rao", "date": "2026-10-07", "time": "10:30"},
                           "Ravi", "SELF")
    assert res == {"status": "slot_taken", "verified": False}


@pytest.mark.asyncio
async def test_reports_held_by_lab_are_not_offered():
    t = KriyaTools(ctx())
    rows = [{"id": "r1", "report_name": "CBC", "status": "needs_review", "file_path": "x.pdf"},
            {"id": "r2", "report_name": "LFT", "status": "sent", "file_path": "y.pdf"}]
    with patch("app.voice.tools.sb", new=_sb_returning(rows)), \
         patch("app.voice.tools.store.add_event", new=AsyncMock()):
        out = await t.reports()
    assert [r["status"] for r in out] == ["processing", "ready"]


@pytest.mark.asyncio
async def test_test_mode_never_writes():
    t = KriyaTools(ctx(mode="test"))
    with patch("app.database.book_appointment", new=AsyncMock()) as ba, \
         patch.object(KriyaTools, "caller_profile", new=AsyncMock(return_value=None)), \
         patch("app.voice.tools.store.add_event", new=AsyncMock()):
        res = await t.book({"doctor_id": "d1", "doctor_name": "Dr. Rao", "date": "2026-10-07", "time": "10:30"},
                           "Ravi", "SELF")
    assert res["simulated"] is True
    ba.assert_not_awaited()


# ---- session pipeline ----

def _session(tools, call_flags=None):
    s = CallSession(ctx(), tools=tools, now=NOW)
    return s, [
        patch("app.voice.session.store.update_call", new=AsyncMock()),
        patch("app.voice.session.store.add_event", new=AsyncMock()),
        patch("app.voice.session.store.get_call", new=AsyncMock(return_value=call_flags or {})),
        patch("app.voice.session.store.load_lexicon", new=AsyncMock(return_value=[])),
        patch("app.database.get_doctors", new=AsyncMock(return_value=DOCS)),
    ]


async def _drive(s, patches, *utterances):
    for p in patches:
        p.start()
    try:
        await s.start()
        return [await s.handle(u) for u in utterances]
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_session_telugu_booking_end_to_end():
    tools = FakeTools(slots=SLOTS[:1] + SLOTS[2:])
    s, p = _session(tools)
    r1, r2, r3 = await _drive(s, p, "Naaku repu heart doctor appointment kavali", "Morning", "avunu")
    assert "ఉదయం కావాలా" in r1.texts[0]
    assert "బుక్ అయింది" in " ".join(r3.texts) and r3.control == "continue"
    assert [c for c in tools.calls if c[0] == "book"]


@pytest.mark.asyncio
async def test_session_emergency_transfers_before_any_nlu():
    tools = FakeTools()
    s, p = _session(tools)
    with patch("app.voice.session.understand") as nlu:
        (r,) = await _drive(s, p, "నాకు ఛాతీలో బాగా నొప్పిగా ఉంది")
    nlu.assert_not_called()
    assert r.control == "transfer" and "108" in r.texts[0]


@pytest.mark.asyncio
async def test_staff_takeover_stops_automation():
    tools = FakeTools(slots=SLOTS)
    s, p = _session(tools, call_flags={"takeover_requested_at": "2026-10-06T09:01:00+05:30"})
    (r,) = await _drive(s, p, "repu heart doctor kavali")
    assert r.control == "transfer" and ("handoff", "staff_takeover") in tools.calls
    assert not [c for c in tools.calls if c[0] == "find_slots"]


# ---- store helpers ----

def test_voice_config_merges_and_hours():
    cfg = store.voice_config({"config": {"voice": {"assistant_name": "Asha",
                                                   "reception_hours": {"end": "18:00"}, "junk": 1}}})
    assert cfg["assistant_name"] == "Asha" and cfg["reception_hours"]["start"] == "09:00"
    assert cfg["reception_hours"]["end"] == "18:00" and "junk" not in cfg
    assert store.within_hours(cfg["reception_hours"], datetime(2026, 10, 6, 10, 0, tzinfo=IST)) is True
    assert store.within_hours(cfg["reception_hours"], datetime(2026, 10, 6, 19, 0, tzinfo=IST)) is False
    assert store.within_hours(cfg["reception_hours"], datetime(2026, 10, 11, 10, 0, tzinfo=IST)) is False  # Sunday


# ---- outbound lead calls ----

def _lead_session(profile):
    s = CallSession(ctx(), tools=FakeTools(profile=profile), now=NOW)
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize("profile, spoken", [
    ({"name": "Ravi Kumar"}, "Ravi Kumar"),
    ({"name": "Chaitu 😎🔥"}, None),          # WhatsApp display names are not spoken
    ({"name": "~"}, None),
    (None, None),
])
async def test_lead_call_greets_by_plain_name_and_preselects_department(profile, spoken):
    s = _lead_session(profile)
    with patch("app.database.get_doctors", new=AsyncMock(return_value=DOCS)), \
         patch("app.voice.session.store.load_lexicon", new=AsyncMock(return_value=[])):
        name, slots = await s._lead_context("heart problem / cardiology")
    assert name == spoken
    assert slots == {"department": "Cardiology"}


@pytest.mark.asyncio
async def test_lead_yes_goes_straight_to_the_day_question():
    s = _lead_session({"name": "Ravi Kumar"})
    with patch("app.database.get_doctors", new=AsyncMock(return_value=DOCS)), \
         patch("app.voice.session.store.load_lexicon", new=AsyncMock(return_value=[])), \
         patch("app.voice.session.store.update_call", new=AsyncMock()), \
         patch("app.voice.session.store.add_event", new=AsyncMock()), \
         patch("app.voice.session.store.get_call", new=AsyncMock(return_value={})), \
         patch("app.voice.session.understand_llm", new=AsyncMock(side_effect=AssertionError("rules must hear this"))):
        g = await s.start(outbound=True, interest="cardiology")
        assert "Ravi Kumar గారు" in g.texts[0]
        r = await s.handle("ఆ, చెప్పండి")
    assert "ఏ రోజు" in " ".join(r.texts)            # department not asked again


def test_stt_auto_detects_language_by_default():
    from app.voice.providers import sarvam
    from urllib.parse import parse_qs, urlparse
    seen = {}

    async def fake_connect(url, **kw):
        seen.update(parse_qs(urlparse(url).query))
        return MagicMock()
    import asyncio
    with patch.object(sarvam, "connect", new=fake_connect):
        asyncio.run(sarvam.SarvamSTT().open("te-IN"))
        assert seen["language-code"] == ["unknown"]
        with patch.object(sarvam.settings, "voice_stt_auto_language", False):
            asyncio.run(sarvam.SarvamSTT().open("te-IN"))
    assert seen["language-code"] == ["te-IN"]
