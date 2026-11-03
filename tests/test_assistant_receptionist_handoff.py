"""Session 56: staff hand work to the AI receptionist by voice through Kriya Ketu.

voice.reminder_calls  appointment reminder calls on / off and when (PUT /admin/voice/settings, `reminders` only)
voice.call_lead       one WhatsApp lead, by name, called by the receptionist (POST /admin/voice/outbound)
Both: preview first, nothing written before Confirm, read back after, VOICE_MANAGE + AI receptionist on.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request

from app.assistant import resolve, rules
from app.assistant.registry import REGISTRY
from tests.test_assistant_lead_calls import (P1, P2, SETTINGS, S, _ask, _card, _confirm, _lead, _reset, fake,  # noqa: F401
                                             lc)
from tests.test_assistant_runs import _user, db  # noqa: F401  (fixtures)
from tests.test_assistant_runs import as_user  # noqa: F401


@fake.post("/admin/voice/outbound")
async def _queue_one(request: Request):
    body = await request.json()
    S["writes"].append(("outbound", body))
    S["jobs"].append({"patient_phone": body["phone"], "status": "queued", "attempts": 0})
    return {"job": {"id": "j1"}}


def _with_reminders(enabled, minutes):
    s = json.loads(json.dumps(SETTINGS))
    s["reminders"] = {"enabled": enabled, "minutes_before": minutes}
    return s


# ── understanding ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, minutes", [
    ("reminder calls 1 hour before", [60]), ("set reminder calls 1 day and 2 hours before", [1440, 120]),
    ("half an hour before", [30]), ("30 minutes before", [30]), ("oka roju mundu", [1440]),
    ("2 ghante pehle", [120]), ("an hour before", [60]), ("turn on reminder calls", None),
    ("1 day, 3 hours and 45 mins and 15 minutes", [1440, 180, 45]),        # at most 3, largest first
])
def test_resolve_reminder_minutes(text, minutes):
    assert resolve.resolve_reminder_minutes(text) == minutes


@pytest.mark.parametrize("text, who", [
    ("make the AI receptionist call Ravi", "ravi"), ("have kriya call Sita Devi about thyroid test", "sita devi"),
    ("ask the receptionist to call lead Sita", "sita"), ("receptionist tho Ravi ki call cheyinchu", "ravi"),
    ("have kriya call the lead ending 4321", "4321"), ("make the AI receptionist call", None),
])
def test_resolve_call_lead(text, who):
    assert resolve.resolve_call_lead(text) == who


@pytest.mark.parametrize("text, action", [
    ("transfer the leads to the AI receptionist", "leads.call"),          # was ipd.transfer
    ("make the AI receptionist call Ravi", "voice.call_lead"),            # was staff.role
    ("make Ravi a receptionist", "staff.role"),
    ("transfer Sita to bed 12", "ipd.transfer"),
    ("lead call results", "leads.funnel"),
    ("ask kriya to call 9876543210", "voice.call_number"),
    ("turn on reminder calls", "voice.reminder_calls"),
    ("turn on automatic lead calls", "voice.auto_leads"),
])
def test_routes(text, action):
    assert rules.route(text).action_id == action


def test_registry_shape():
    for a in ("voice.reminder_calls", "voice.call_lead"):
        spec = REGISTRY[a]
        assert spec.risk == "R2" and spec.permission == "VOICE_MANAGE"
    assert REGISTRY["voice.reminder_calls"].endpoint == ("PUT", "/admin/voice/settings")
    assert REGISTRY["voice.call_lead"].endpoint == ("POST", "/admin/voice/outbound")


# ── reminder calls end to end ────────────────────────────────────────────────

def test_reminder_calls_on_with_times_sends_only_reminders_changed(db, lc):  # noqa: F811
    _reset(settings=_with_reminders(False, [60]))
    out = _ask("switch on appointment reminder calls 1 day and 2 hours before")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "1 day, 2 hours before" in json.dumps(_card(out)) and S["writes"] == []
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    (kind, body), = S["writes"]
    want = _with_reminders(True, [1440, 120])
    assert body == want                                                     # everything else sent back unchanged


def test_reminder_calls_on_keeps_the_saved_times(db, lc):  # noqa: F811
    _reset(settings=_with_reminders(False, [180, 30]))
    out = _ask("turn on reminder calls")
    _confirm(out)
    assert S["writes"][0][1]["reminders"] == {"enabled": True, "minutes_before": [180, 30]}


def test_reminder_calls_off(db, lc):  # noqa: F811
    _reset(settings=_with_reminders(True, [60]))
    out = _ask("turn off reminder calls")
    assert "WhatsApp reminders continue" in json.dumps(_card(out))
    _confirm(out)
    assert S["writes"][0][1]["reminders"] == {"enabled": False, "minutes_before": [60]}


@pytest.mark.parametrize("text, settings, says", [
    ("turn on reminder calls", _with_reminders(True, [60]), "already on"),
    ("reminder calls 5 minutes before", _with_reminders(False, [60]), "15 minutes to 1 day"),
    ("call tomorrow's patients to remind them", _with_reminders(False, [60]), "turn on reminder calls"),
])
def test_reminder_calls_refusals_write_nothing(db, lc, text, settings, says):  # noqa: F811
    _reset(settings=settings)
    out = _ask(text)
    assert out["error_code"] == "nothing_to_do" and says in out["message"] and S["writes"] == []


def test_reminder_calls_need_voice_manage(db, lc):  # noqa: F811
    lc["user"] = _user()
    out = _ask("turn on reminder calls")
    assert out["state"] == "REJECTED" and S["writes"] == []


# ── one lead by name end to end ──────────────────────────────────────────────

def test_call_lead_by_name_queues_one_call_with_their_interest(db, lc):  # noqa: F811
    _reset(leads=[_lead("Ravi Kumar", P1, ("Knee pain",)), _lead("Sita", P2)])
    out = _ask("make the AI receptionist call Ravi")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "Ravi Kumar" in json.dumps(_card(out)) and S["writes"] == []
    assert P1 not in json.dumps(db.tables["assistant_runs"])                # no phone stored
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("outbound", {"phone": P1, "purpose": "lead_followup", "interest": "Knee pain"})]


def test_call_lead_said_interest_wins(db, lc):  # noqa: F811
    _reset(leads=[_lead("Sita Devi", P2, ("Dental",))])
    out = _ask("have kriya call Sita about thyroid test")
    _confirm(out)
    assert S["writes"][0][1]["interest"] == "thyroid test"


@pytest.mark.parametrize("leads, jobs, says", [
    ([], [], "No WhatsApp lead matches"),
    ([_lead("Ravi Kumar", P1), _lead("Ravi Teja", P2)], [], "2 leads match"),
    ([_lead("Ravi", "+919•••••0009")], [], "asked not to be contacted"),
    ([_lead("Ravi", P1)], [{"patient_phone": P1, "status": "queued"}], "already in the queue"),
])
def test_call_lead_refusals_write_nothing(db, lc, leads, jobs, says):  # noqa: F811
    _reset(leads=leads, jobs=jobs)
    out = _ask("make the AI receptionist call Ravi")
    assert out["error_code"] == "nothing_to_do" and says in out["message"] and S["writes"] == []


def test_call_lead_off_when_the_receptionist_is_off(db, lc):  # noqa: F811
    clinic = {"id": "c", "plan": "polyclinic", "account_type": "tenant", "features": {"kriya_ketu": True}}
    from app.routers import assistant as assistant_router
    with patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=clinic)):
        out = _ask("make the AI receptionist call Ravi")
    assert out["state"] != "NEEDS_CONFIRMATION" and S["writes"] == []


@pytest.mark.parametrize("text", ["give me today's leads", "pass me the leads list", "hand me the leads report",
                                  "follow up leads list", "show me leads to follow up", "what do the leads want"])
def test_a_list_or_a_report_of_leads_is_never_a_call(text):
    """Session 57 audit: loose hand-off phrasings ("give+leads", "pass+leads") read these as CALL the leads."""
    r = rules.route(text)
    assert r.action_id not in ("leads.call", "voice.call_lead"), (text, r.action_id)
