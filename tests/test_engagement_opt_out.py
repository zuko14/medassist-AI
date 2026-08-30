"""Opt-out must actually suppress engagement messages.

`opted_in` was written on opt-out and read by nothing. A patient who sent
"STOP" was told "You've been unsubscribed" and then kept receiving post-visit
follow-ups and day+3/day+7 health check-ins — a broken promise to the patient
and a WhatsApp quality-rating risk, since blocks and reports are what Meta
scores a WABA on.

Scope decision pinned by these tests: opt-out suppresses ENGAGEMENT only.
Appointment reminders, lab reports and payment messages are transactional —
the patient set them in motion — and must keep flowing, or an opted-out
patient misses the appointment they booked.
"""

from contextlib import asynccontextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.consent import consent_service
from app.services.scheduler import SchedulerService

CLINIC = {"id": "clinic-1", "name": "Apollo Clinic", "plan": "polyclinic"}
PHONE = "+919876543210"


@asynccontextmanager
async def _lock_granted(*args, **kwargs):
    yield True


def _appt(**kw):
    base = {
        "id": "appt-1", "clinic_id": "clinic-1", "patient_phone": PHONE,
        "patient_name": "Anand Rao", "appointment_date": "2026-08-25",
        "status": "completed", "followup_sent": False,
    }
    base.update(kw)
    return base


# ── The shared check ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patient,expected",
    [
        ({"opted_in": False}, False),   # explicit opt-out is the only suppressor
        ({"opted_in": True}, True),
        ({"opted_in": None}, True),     # NULL flag on a legacy row
        ({}, True),                     # column absent from the projection
        (None, True),                   # no patient row at all
    ],
)
async def test_only_an_explicit_opt_out_suppresses_engagement(patient, expected):
    """A data gap must never mute a whole clinic's follow-ups."""
    with patch("app.services.consent.get_patient_by_phone",
               AsyncMock(return_value=patient)):
        assert await consent_service.accepts_engagement("clinic-1", PHONE) is expected


@pytest.mark.asyncio
async def test_a_database_error_propagates_rather_than_guessing():
    """Callers retry; they must not burn a send flag on an answer never got."""
    with patch("app.services.consent.get_patient_by_phone",
               AsyncMock(side_effect=RuntimeError("supabase down"))):
        with pytest.raises(RuntimeError):
            await consent_service.accepts_engagement("clinic-1", PHONE)


# ── Post-visit follow-ups ────────────────────────────────────────────────────


async def _run_followups(opted_in, has_reminders_feature=True):
    """Drive send_followups() with one completed appointment.

    has_feature MUST be patched. Without it the CLINIC fixture's plan does not
    include "reminders", so send_followups() takes the feature-gate branch at
    scheduler.py:632 and returns before ever consulting consent. Both tests in
    this pair then pass or fail for the wrong reason: the opted-out case
    asserted send.assert_not_awaited() and was satisfied by a code path that
    never looked at opted_in at all (KA-P3-14).
    """
    db = MagicMock()
    table = MagicMock()

    # The visit date MUST be relative to today. send_followups() only acts on
    # appointments whose age is between cfg["days"] (1) and
    # cfg["days"] + FOLLOWUP_LOOKBACK_DAYS (4); anything older is past its
    # retry window and burned at scheduler.py:690 before consent is consulted.
    # _appt() hardcodes 2026-08-25, so this helper went silently vacuous the
    # day the clock passed 2026-08-29 — a time-bomb fixture. Two days ago sits
    # in the middle of the window and stays there forever.
    visit_date = (date.today() - timedelta(days=2)).isoformat()

    (table.select.return_value.eq.return_value.eq.return_value
        .gte.return_value.lte.return_value.limit.return_value
        .execute.return_value) = MagicMock(data=[_appt(appointment_date=visit_date)])
    db.table.return_value = table
    send = AsyncMock(return_value=True)

    with patch("app.services.distributed_lock.distributed_job_lock", _lock_granted), \
         patch("app.services.scheduler.supabase", db), \
         patch("app.services.scheduler.whatsapp_service.send_template", send), \
         patch("app.services.scheduler.get_clinic_by_id", AsyncMock(return_value=CLINIC)), \
         patch("app.services.tenant.has_feature",
               MagicMock(return_value=has_reminders_feature)), \
         patch("app.services.scheduler.followup_config",
               MagicMock(return_value={"enabled": True, "days": 1,
                                       "template": "followup_message",
                                       "message": None, "message_template": None})), \
         patch("app.services.consent.get_patient_by_phone",
               AsyncMock(return_value={"opted_in": opted_in})):
        await SchedulerService().send_followups()

    return send, [c.args[0] for c in table.update.call_args_list]


@pytest.mark.asyncio
async def test_opted_out_patient_gets_no_post_visit_followup():
    send, updates = await _run_followups(opted_in=False)

    send.assert_not_awaited()
    # Flag burned so the daily scan stops reconsidering them.
    assert any(u.get("followup_sent") is True for u in updates)


@pytest.mark.asyncio
async def test_opted_in_patient_still_gets_their_followup():
    """The gate must not become a blanket kill switch."""
    send, updates = await _run_followups(opted_in=True)

    send.assert_awaited_once()
    assert any(u.get("followup_sent") is True for u in updates)


@pytest.mark.asyncio
async def test_feature_gate_is_distinct_from_the_consent_gate():
    """A clinic without the reminders feature is skipped for the FEATURE reason.

    Pins the two gates apart. Before KA-P3-14 the feature gate was silently
    doing the consent gate's job in these tests, so a regression in consent
    handling would not have failed anything.
    """
    send, updates = await _run_followups(opted_in=True, has_reminders_feature=False)

    send.assert_not_awaited()
    # Crucially it must NOT burn followup_sent — see the next test.
    assert not any(u.get("followup_sent") is True for u in updates)


@pytest.mark.asyncio
async def test_plan_upgrade_does_not_lose_pending_followups():
    """KA-P3-12: the feature gate must skip, never burn.

    Marking followup_sent=True for a clinic that lacks the feature is
    permanent. When the clinic upgrades, every patient seen before the upgrade
    would already be flagged as followed up and could never receive one.
    """
    # Before the upgrade: skipped, nothing burned.
    send_before, updates_before = await _run_followups(
        opted_in=True, has_reminders_feature=False
    )
    send_before.assert_not_awaited()
    assert not any(u.get("followup_sent") is True for u in updates_before), (
        "followup_sent was burned for a feature-gated clinic — after a plan "
        "upgrade this patient can never receive a follow-up"
    )

    # After the upgrade: the same appointment is still eligible and is sent.
    send_after, updates_after = await _run_followups(
        opted_in=True, has_reminders_feature=True
    )
    send_after.assert_awaited_once()
    assert any(u.get("followup_sent") is True for u in updates_after)


# ── Health check-ins ─────────────────────────────────────────────────────────


async def _run_checkins(opted_in):
    db = MagicMock()
    table = MagicMock()
    (table.select.return_value.eq.return_value.eq.return_value
        .eq.return_value.execute.return_value) = MagicMock(
            data=[_appt(status="confirmed", doctor_name="Dr. Rao")])
    db.table.return_value = table
    send = AsyncMock(return_value=True)

    with patch("app.services.distributed_lock.distributed_job_lock", _lock_granted), \
         patch("app.services.scheduler.supabase", db), \
         patch("app.services.scheduler.whatsapp_service.send_interactive_buttons", send), \
         patch("app.services.scheduler.get_clinic_by_id", AsyncMock(return_value=CLINIC)), \
         patch("app.services.consent.get_patient_by_phone",
               AsyncMock(return_value={"opted_in": opted_in})):
        await SchedulerService().send_health_checkins()

    return send


@pytest.mark.asyncio
async def test_opted_out_patient_gets_no_health_checkin():
    send = await _run_checkins(opted_in=False)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_opted_in_patient_still_gets_their_health_checkin():
    send = await _run_checkins(opted_in=True)
    assert send.await_count >= 1


# ── Transactional messages are deliberately NOT suppressed ───────────────────


@pytest.mark.asyncio
async def test_opted_out_patient_still_gets_appointment_reminders():
    """Silencing a reminder would make an opted-out patient miss their slot."""
    db = MagicMock()
    table = MagicMock()
    (table.select.return_value.eq.return_value.eq.return_value
        .eq.return_value.execute.return_value) = MagicMock(
            data=[_appt(status="confirmed", doctor_name="Dr. Rao",
                        appointment_time="10:00", reminder_24h_sent=False)])
    db.table.return_value = table
    send = AsyncMock(return_value=True)

    with patch("app.services.distributed_lock.distributed_job_lock", _lock_granted), \
         patch("app.services.scheduler.supabase", db), \
         patch("app.services.scheduler.whatsapp_service.send_template", send), \
         patch("app.services.scheduler.get_clinic_by_id", AsyncMock(return_value=CLINIC)), \
         patch("app.services.consent.get_patient_by_phone",
               AsyncMock(return_value={"opted_in": False})):
        await SchedulerService().send_24h_reminders()

    send.assert_awaited_once()


def test_opt_out_confirmation_describes_what_actually_stops():
    """The old wording promised total silence the system never delivered."""
    from app.templates.whatsapp_templates import get_message

    en = get_message("opt_out_confirm", "en").lower()
    assert "reminder" in en          # says what still arrives
    assert "follow-up" in en         # says what stops
    for lang in ("en", "hi", "te"):
        assert get_message("opt_out_confirm", lang)
