"""Continuous Care latent fixes (P0b; docs/continuous-care/TEST-PLAN.md §2).

CC-L1: a self-harm phrase (en / hi / te, script and Latin, inside longer text) gets the crisis card from the main
menu, mid-booking and the symptom states, and the conversation stops there; ordinary words never trigger.
CC-L2: the legacy free-text medication reminders skip a patient who sent STOP and count undelivered sends.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services import crisis
from app.services.conversation import ConversationManager

CLINIC = {"id": "clinic-1", "name": "Apollo Clinic", "phone": "+919999999999", "config": {}, "features": {}}
PHONE = "+919876543210"

TRIGGERS = [
    "I want to kill myself", "thinking about SUICIDE again", "i just want to end my life.", "I wish I was dead",
    "sometimes i feel i'd be better off dead", "I don’t want to live anymore", "i will hurt myself",
    "मैं आत्महत्या करना चाहता हूँ", "अब जीना नहीं चाहती", "mujhe khudkushi karni hai", "main marna chahta hoon",
    "jeena nahi chahti", "నాకు ఆత్మహత్య చేసుకోవాలని ఉంది", "నాకు బ్రతకాలని లేదు", "naaku chanipovalani undi",
    "doctor please help, I want to die, nobody listens",
]
SAFE = ["I am on a diet", "die-cast toy for my son", "I studied the report", "my diet plan please", "book appointment",
        "suicidality", "killing time before the visit", "dietician", "Hi", "Cardiology", "kill the pain please"]


@pytest.mark.parametrize("text", TRIGGERS)
def test_self_harm_phrases_trigger(text):
    assert crisis.detect(text)


@pytest.mark.parametrize("text", SAFE)
def test_ordinary_words_do_not_trigger(text):
    assert not crisis.detect(text)


def test_card_has_tele_manas_emergency_and_112_in_each_language():
    for lang in ("en", "hi", "te", "xx"):
        card = crisis.card_text(lang, "Apollo Clinic", "+91 40 1234")
        assert "14416" in card and "+91 40 1234" in card and "112" in card and "Apollo Clinic" in card
    assert crisis.card_text("te", "", "") .count("112") >= 1


def _patches(state):
    session = {"state": state, "context": {}, "last_processed_message_id": None, "booking_context_expires_at": None}
    patient = {"id": "p1", "phone": PHONE, "language": "hi", "visit_count": 1}
    return [patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=session)),
            patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value=patient)),
            patch("app.services.conversation.update_conversation", new=AsyncMock()),
            patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="unknown"))]


async def _run(text, state):
    manager = ConversationManager()
    manager.whatsapp = AsyncMock()
    respond = AsyncMock()
    ps = _patches(state) + [patch.object(crisis, "respond", new=respond)]
    for p in ps:
        p.start()
    try:
        try:
            await manager._handle_message_locked(clinic=CLINIC, phone=PHONE, message=text, message_type="text",
                                                 message_id="wamid.CRISIS1")
        except Exception:                      # the normal pipeline past the guard is not under test here
            if crisis.detect(text):
                raise
    finally:
        for p in ps:
            p.stop()
    return respond, manager


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["main_menu", "idle", "collecting_name", "selecting_slot", "collecting_symptoms",
                                   "asking_symptoms"])
async def test_crisis_card_from_any_state_and_nothing_else_answers(state):
    respond, manager = await _run("I don't want to live, please help", state)
    respond.assert_awaited_once()
    assert respond.await_args.args[1] is CLINIC and respond.await_args.args[2] == PHONE
    assert respond.await_args.args[3] == "hi"                       # the patient's language
    manager.whatsapp.send_text.assert_not_awaited()                  # the card is the only reply


@pytest.mark.asyncio
async def test_normal_message_never_reaches_the_crisis_path():
    respond, _ = await _run("I am on a diet", "main_menu")
    respond.assert_not_awaited()


@pytest.mark.asyncio
async def test_respond_sends_card_and_masked_alert_and_survives_failures():
    manager = MagicMock()
    manager.whatsapp.send_text = AsyncMock(side_effect=RuntimeError("meta down"))
    inserted = []

    class _T:
        def insert(self, row):
            inserted.append(row)
            return self

    async def _sb(q):
        return SimpleNamespace(data=[{}])
    with patch("app.database.supabase", MagicMock(table=lambda n: _T())), patch("app.database.sb", new=_sb):
        await crisis.respond(manager, {**CLINIC, "features": {}}, PHONE, "en")   # card failure does not stop the alert
    assert inserted and inserted[0]["clinic_id"] == "clinic-1" and "[URGENT]" in inserted[0]["title"]
    assert PHONE not in inserted[0]["message"] and "7654" not in inserted[0]["message"][:0]
    assert "I don't want" not in inserted[0]["message"]                 # never the message text


# ── CC-L2 ────────────────────────────────────────────────────────────────────

class _Q:
    def __init__(self, table):
        self.table = table

    def __getattr__(self, name):
        return lambda *a, **k: self


def _rx(i, phone):
    now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%H:%M")
    return {"id": f"rx{i}", "clinic_id": "clinic-1", "patient_phone": phone, "patient_name": "Lakshmi",
            "medicine_name": "Metformin", "dosage": "500 mg", "reminder_times": [now]}


@pytest.mark.asyncio
async def test_legacy_reminders_skip_opted_out_and_count_undelivered():
    from app.services import prescriptions as mod
    rows = [_rx(1, "+911111111111"), _rx(2, "+912222222222"), _rx(3, "+913333333333")]

    async def _sb(q):
        return SimpleNamespace(data=rows if q.table == "prescriptions" else [])

    class _Lock:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return True

        async def __aexit__(self, *a):
            return False

    consent = MagicMock()
    consent.return_value.accepts_engagement = AsyncMock(side_effect=lambda cid, phone: phone != "+912222222222")
    send = AsyncMock(side_effect=lambda clinic, phone, msg, **k: phone != "+913333333333")
    with patch.object(mod, "sb", new=_sb), patch.object(mod, "scoped_query", new=lambda t, **k: _Q(t)), \
            patch.object(mod, "supabase", MagicMock(table=lambda t: _Q(t))), \
            patch.object(mod, "get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
            patch.object(mod, "automated_outbound_allowed", new=lambda c: True), \
            patch.object(mod.whatsapp_service, "send_text", new=send), \
            patch("app.services.distributed_lock.distributed_job_lock", new=_Lock), \
            patch("app.services.consent.ConsentService", new=consent):
        out = await mod.PrescriptionService().send_due_reminders()
    assert out["opted_out"] == 1 and out["undelivered"] == 1 and out["sent"] == 2 and out["errors"] == 0
    assert [c.args[1] for c in send.await_args_list] == ["+911111111111", "+913333333333"]   # STOP never messaged


@pytest.mark.asyncio
async def test_legacy_reminders_still_send_when_the_consent_lookup_fails():
    from app.services import prescriptions as mod
    rows = [_rx(1, "+911111111111")]

    async def _sb(q):
        return SimpleNamespace(data=rows if q.table == "prescriptions" else [])

    class _Lock:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return True

        async def __aexit__(self, *a):
            return False
    consent = MagicMock()
    consent.return_value.accepts_engagement = AsyncMock(side_effect=RuntimeError("db"))
    send = AsyncMock(return_value=True)
    with patch.object(mod, "sb", new=_sb), patch.object(mod, "scoped_query", new=lambda t, **k: _Q(t)), \
            patch.object(mod, "supabase", MagicMock(table=lambda t: _Q(t))), \
            patch.object(mod, "get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
            patch.object(mod, "automated_outbound_allowed", new=lambda c: True), \
            patch.object(mod.whatsapp_service, "send_text", new=send), \
            patch("app.services.distributed_lock.distributed_job_lock", new=_Lock), \
            patch("app.services.consent.ConsentService", new=consent):
        out = await mod.PrescriptionService().send_due_reminders()
    assert out["sent"] == 1 and out["opted_out"] == 0
