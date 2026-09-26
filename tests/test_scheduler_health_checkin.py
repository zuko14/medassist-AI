"""Day+3 / day+7 health check-ins (fixed 2026-09-26, opt-in per clinic).

The previous job could never deliver: it selected status='confirmed' visits
that auto_complete_appointments had already marked 'completed', and it sent a
free-form message Meta refuses outside the 24h window."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scheduler import (
    HEALTH_CHECKIN_PAYLOADS,
    SchedulerService,
    health_checkin_config,
)

ON = {"id": "clinic-1", "name": "Test Hospital", "plan": "polyclinic",
      "config": {"health_checkins_enabled": True}}


@asynccontextmanager
async def _lock(*args, **kwargs):
    yield True


def _appt(**kw):
    a = {"id": "appt-1", "clinic_id": "clinic-1", "patient_phone": "+919876543210",
         "patient_name": "Ravi Kumar", "doctor_name": "Rao", "status": "completed",
         "booking_type": "consultation"}
    a.update(kw)
    return a


def _db(rows_3d, rows_7d=()):
    """Self-returning builder recording every call; first execute = day+3 sweep."""
    calls = []
    q = MagicMock()

    def rec(name):
        def _m(*a, **k):
            calls.append((name, a))
            return q
        return _m

    for m in ("select", "eq", "gte", "lte", "in_", "limit", "update"):
        getattr(q, m).side_effect = rec(m)
    q.execute.side_effect = [MagicMock(data=list(rows_3d)), MagicMock(data=list(rows_7d))] + [MagicMock(data=[])] * 20
    db = MagicMock()
    db.table.return_value = q
    return db, calls


async def _run(rows, clinic=ON, template_ok=True, interactive_ok=False, engaged=True):
    db, calls = _db(rows)
    tpl = AsyncMock(return_value=template_ok)
    inter = AsyncMock(return_value=interactive_ok)
    with patch("app.services.distributed_lock.distributed_job_lock", _lock), \
         patch("app.services.scheduler.supabase", db), \
         patch("app.services.scheduler.get_clinic_by_id", AsyncMock(return_value=clinic)), \
         patch("app.services.scheduler.whatsapp_service.send_template", tpl), \
         patch("app.services.scheduler.whatsapp_service.send_interactive_buttons", inter), \
         patch("app.services.consent.consent_service.accepts_engagement", AsyncMock(return_value=engaged)):
        await SchedulerService().send_health_checkins()
    updates = [a[0] for n, a in calls if n == "update"]
    return tpl, inter, updates, calls


def test_off_by_default():
    assert health_checkin_config({})["enabled"] is False
    assert health_checkin_config(ON)["enabled"] is True
    assert health_checkin_config({"config": {"health_checkin_template_name": "x"}})["template"] == "x"


@pytest.mark.asyncio
async def test_selects_visits_actually_seen_not_only_confirmed():
    _, _, _, calls = await _run([])
    assert ("in_", ("status", ["confirmed", "completed"])) in calls  # the root-cause fix
    assert ("eq", ("booking_type", "consultation")) in calls           # lab bookings have no doctor
    assert any(n == "gte" and a[0] == "appointment_date" for n, a in calls)  # retry window


@pytest.mark.asyncio
async def test_sends_template_with_routable_buttons_and_marks_flag():
    tpl, inter, updates, _ = await _run([_appt()])
    tpl.assert_awaited_once()
    args, kwargs = tpl.await_args
    assert args[1] == "+919876543210" and args[2] == "patient_health_checkin"
    body = kwargs["components"][0]["parameters"]
    assert [p["text"] for p in body] == ["Ravi", "Dr. Rao"]
    payloads = [c["parameters"][0]["payload"] for c in kwargs["components"] if c["type"] == "button"]
    assert tuple(payloads) == HEALTH_CHECKIN_PAYLOADS
    inter.assert_not_awaited()
    assert {"health_checkin_3d_sent": True} in updates


@pytest.mark.asyncio
async def test_template_refused_falls_back_to_in_window_interactive():
    tpl, inter, updates, _ = await _run([_appt()], template_ok=False, interactive_ok=True)
    inter.assert_awaited_once()
    assert {"health_checkin_3d_sent": True} in updates


@pytest.mark.asyncio
async def test_nothing_delivered_leaves_flag_for_tomorrows_retry():
    _, _, updates, _ = await _run([_appt()], template_ok=False, interactive_ok=False)
    assert updates == []


@pytest.mark.asyncio
async def test_clinic_that_has_not_opted_in_is_never_messaged():
    tpl, inter, updates, _ = await _run([_appt()], clinic={**ON, "config": {}})
    tpl.assert_not_awaited()
    inter.assert_not_awaited()
    assert {"health_checkin_3d_sent": True} in updates  # burned: no late burst on switch-on


@pytest.mark.asyncio
async def test_dental_sitting_is_skipped():
    tpl, _, updates, _ = await _run([_appt(treatment_plan_id="plan-1")])
    tpl.assert_not_awaited()
    assert {"health_checkin_3d_sent": True} in updates


@pytest.mark.asyncio
async def test_plan_without_reminders_feature_is_skipped_but_not_burned():
    tpl, _, updates, _ = await _run([_appt()], clinic={**ON, "plan": "diagstream"})
    tpl.assert_not_awaited()
    assert updates == []


@pytest.mark.asyncio
async def test_opted_out_patient_is_burned_not_messaged():
    tpl, _, updates, _ = await _run([_appt()], engaged=False)
    tpl.assert_not_awaited()
    assert {"health_checkin_3d_sent": True} in updates


@pytest.mark.asyncio
async def test_template_button_taps_reach_the_checkin_handlers():
    """A template quick-reply (message_type 'button') with our payload must be
    routed exactly like the in-chat button of the same id."""
    from app.services import conversation

    assert conversation.TEMPLATE_BUTTON_PAYLOADS == frozenset(HEALTH_CHECKIN_PAYLOADS)
    src = open(conversation.__file__, encoding="utf-8").read()
    assert 'interactive_data.get("id") in TEMPLATE_BUTTON_PAYLOADS' in src
    assert 'elif button_id == "checkin_concern":' in src and 'elif button_id == "checkin_ok":' in src


def test_followup_tells_patients_to_call_their_own_clinic():
    """The built-in follow-up used settings.hospital_phone, ONE platform-wide
    number, so every clinic's patients were told to call the same number."""
    from app.services.scheduler import clinic_callback_phone

    cfg = {"message": "", "message_template": "", "template": "post_appointment_followup"}
    clinic = {"whatsapp_number": "+919000000001", "config": {"staff_phone": "+919000000002"}}
    _, comps = SchedulerService._followup_template_and_components(cfg, "Ravi", clinic_callback_phone(clinic))
    assert [p["text"] for p in comps[0]["parameters"]] == ["Ravi", "+919000000002"]
    assert clinic_callback_phone({"whatsapp_number": "+919000000001", "config": {}}) == "+919000000001"
