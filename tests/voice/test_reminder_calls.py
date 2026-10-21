"""Appointment reminder calls (migration 112): when they are queued, what Kriya says,
what happens on yes / no, and the guards at dial time (too late, appointment moved)."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.voice import outbound
from app.voice import responses as R
from app.voice.dates import IST

from .test_dialog import TODAY, Conv, FakeTools
from tests.config_writes import captured_config_writes

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=IST)
APPT = {"id": "a1", "patient_phone": "+919000000001", "patient_name": "Lakshmi", "doctor_id": "d1",
        "doctor_name": "Dr. Srinivas Rao", "booking_type": "consultation", "status": "confirmed",
        "appointment_date": "2026-10-06", "appointment_time": "17:30:00",
        "created_at": "2026-10-01T10:00:00+00:00"}
REM = {"id": "a1", "booking_ref": "KR-1", "status": "confirmed", "paid": False, "doctor_id": "d1",
       "doctor_name": "Dr. Srinivas Rao", "booking_type": "consultation", "lab_test_name": None,
       "date": "2026-10-06", "time": "17:30", "patient_name": "Lakshmi"}
CLINIC = {"id": "c1", "features": {"ai_receptionist": True}}


def _res(data):
    return SimpleNamespace(data=data)


# ---- when a reminder is due ----

def test_offsets_are_validated_deduplicated_largest_first():
    assert outbound.reminder_offsets({"reminders": {"minutes_before": [60, 1440, 60, 5, 2000, True, "30", 15]}}) \
        == [1440, 60, 15]
    assert outbound.reminder_offsets({}) == []


def test_due_only_once_the_moment_has_come_and_before_call_by():
    at = datetime(2026, 10, 6, 17, 30, tzinfo=IST)
    assert outbound.reminder_due(APPT, [60], at - timedelta(minutes=61)) is None
    assert outbound.reminder_due(APPT, [60], at - timedelta(minutes=60)) == (60, at - timedelta(minutes=10))
    assert outbound.reminder_due(APPT, [60], at - timedelta(minutes=10)) is None        # too close to the visit
    assert outbound.reminder_due({**APPT, "appointment_time": None}, [60], NOW) is None


def test_after_downtime_only_the_latest_due_offset_is_called():
    at = datetime(2026, 10, 6, 17, 30, tzinfo=IST)
    assert outbound.reminder_due(APPT, [1440, 120, 30], at - timedelta(minutes=100))[0] == 120
    assert outbound.reminder_due(APPT, [1440, 120, 30], at - timedelta(minutes=20))[0] == 30


def test_an_offset_that_passed_before_the_booking_is_skipped():
    booked_late = {**APPT, "created_at": "2026-10-06T16:00:00+05:30"}           # 90 min before the visit
    assert outbound.reminder_due(booked_late, [1440], NOW.replace(hour=16, minute=5)) is None
    assert outbound.reminder_due(booked_late, [1440, 60], NOW.replace(hour=16, minute=31))[0] == 60


def test_reminder_key_changes_when_the_appointment_moves():
    assert outbound.reminder_key(APPT, 60) == "a1|2026-10-06T17:30|60"
    assert outbound.reminder_key({**APPT, "appointment_time": "18:00"}, 60) != outbound.reminder_key(APPT, 60)


# ---- what Kriya says ----

def _conv(lang="te-IN", reminder=REM):
    c = Conv(FakeTools(appts=[]), lang=lang)
    return c, c.text(c.engine.greeting(c.state, reminder=reminder))


def test_telugu_greeting_names_hospital_patient_doctor_day_and_time():
    _, te = _conv()
    assert te.startswith("నమస్కారం, నేను ABC Hospitals నుండి Kriya మాట్లాడుతున్నాను.")
    assert "Lakshmi గారికి ఈ రోజు సాయంత్రం 5:30కి డాక్టర్ Srinivas Rao గారితో అపాయింట్మెంట్ ఉంది." in te
    assert "సమయానికి" in te and te.endswith("మీరు వస్తున్నారా అండి?")


def test_greeting_without_a_patient_name_says_you():
    _, en = _conv("en-IN", {**REM, "patient_name": None})
    assert "a reminder that you have an appointment with Dr. Srinivas Rao today at 5:30 PM." in en
    _, hi = _conv("hi-IN", {**REM, "patient_name": None})
    assert "आपका आज शाम 5:30 बजे डॉक्टर Srinivas Rao के साथ अपॉइंटमेंट है।" in hi


def test_new_templates_render_without_placeholders():
    for key in ("greeting_reminder", "reminder_ask_coming", "reminder_confirmed", "no_problem", "offer_reschedule"):
        for lang in ("te-IN", "hi-IN", "en-IN"):
            out = R.realize(key, lang, TODAY, {}, hospital="H", assistant="K", appt=REM, patient="Lakshmi")
            assert out and "{" not in out, (key, lang, out)


def test_yes_thanks_them_and_ends_the_call():
    c, _ = _conv("en-IN")
    reply = c.say("Yes, I will come")
    assert reply == "Great, thank you. We look forward to seeing you."
    assert c.control == "end"
    assert c.state["outcomes"] == [{"wf": "REMINDER", "outcome": "reminder_confirmed"}]


def test_cannot_come_offers_to_move_this_appointment_without_asking_which():
    # Two upcoming visits on this number: the generic picker would have to ask "which one?".
    other = {**REM, "id": "a0", "date": "2026-10-07", "doctor_id": "d3", "doctor_name": "Dr. Lakshmi Prasanna"}
    c = Conv(FakeTools(appts=[other, REM]), lang="en-IN")
    c.engine.greeting(c.state, reminder=REM)
    reply = c.say("No")
    assert reply == "No problem. Shall I move your appointment to another day or time?"
    assert c.state["expect"] == "offer" and c.control == "continue"
    assert c.state["outcomes"][-1]["outcome"] == "reminder_cant_come"
    reply = c.say("Yes")
    assert c.state["task"]["intent"] == "RESCHEDULE_APPOINTMENT"
    assert c.state["task"]["slots"]["reschedule_of"]["id"] == "a1"
    assert reply == "Which day would you like?"           # straight to the new day, no "which appointment?"


def test_unclear_answer_repeats_only_the_short_question():
    c, _ = _conv("en-IN")
    assert c.say("Hello?") == "Will you be able to come for the appointment?"
    assert c.state["expect"] == "reminder"


def test_a_cancel_request_on_a_reminder_call_uses_the_normal_cancel_flow():
    c = Conv(FakeTools(appts=[REM]), lang="en-IN")
    c.engine.greeting(c.state, reminder=REM)
    reply = c.say("Please cancel my appointment")
    assert c.state["task"]["wf"] == "CANCEL" and "cancel" in reply.lower()


# ---- dial time ----

def _job(call_by_in=timedelta(hours=1), **kw):
    return {"id": "j1", "clinic_id": "c1", "patient_phone": "+919000000001", "attempts": 0,
            "purpose": "appointment_reminder", "created_by": "auto_reminders",
            "context": {"appointment": REM, "call_by": (datetime.now(IST) + call_by_in).isoformat()}, **kw}


def _patches(sb, **kw):
    return [patch.object(outbound, "get_clinic_by_id", new=AsyncMock(return_value=CLINIC)),
            patch.object(outbound, "ai_receptionist_enabled", return_value=True),
            patch.object(outbound, "next_window_start", return_value=kw.get("later")),
            patch.object(outbound, "_today_count", new=AsyncMock(return_value=10_000)),   # cap is never consulted
            patch.object(outbound, "_in_flight", new=AsyncMock(return_value=0)),
            patch.object(outbound, "_eligible", new=AsyncMock(return_value=None)),
            patch.object(outbound, "_reminder_still_valid", new=AsyncMock(return_value=kw.get("valid", True))),
            patch.object(outbound.store, "create_call",
                         new=kw.get("create_call") or AsyncMock(return_value={"id": "call1", "clinic_id": "c1"})),
            patch.object(outbound.store, "update_call", new=AsyncMock()),
            patch.object(outbound, "_dial", new=kw.get("dial") or AsyncMock(return_value="SID1")),
            patch.object(outbound, "sb", new=sb)]


async def _dispatch(sb, **kw):
    from contextlib import ExitStack
    with ExitStack() as st, patch.object(outbound, "supabase", new=MagicMock()) as fake:
        for p in _patches(sb, **kw):
            st.enter_context(p)
        placed = await outbound.dispatch_due()
    return placed, fake


@pytest.mark.asyncio
async def test_valid_reminder_is_dialled_with_the_appointment_and_ignores_the_lead_cap():
    sb = AsyncMock(side_effect=[_res([_job()]), _res([{"exophone": "+914000000000"}]), _res([{"id": "j1"}]), _res([])])
    cc = AsyncMock(return_value={"id": "call1"})
    placed, _ = await _dispatch(sb, create_call=cc)
    assert placed == 1
    assert cc.await_args.args[1]["handoff_packet"] == {"reminder": REM}


@pytest.mark.asyncio
async def test_reminder_past_call_by_or_window_past_call_by_is_skipped_not_deferred():
    for job, later in ((_job(call_by_in=timedelta(minutes=-1)), None),
                       (_job(), datetime.now(IST) + timedelta(hours=3))):
        dial = AsyncMock()
        placed, fake = await _dispatch(AsyncMock(side_effect=[_res([job]), _res([])]), later=later, dial=dial)
        assert placed == 0
        dial.assert_not_called()
        assert fake.table.return_value.update.call_args[0][0] == {"status": "skipped", "last_error": "too_late"}


@pytest.mark.asyncio
async def test_cancelled_or_moved_appointment_is_not_called():
    dial = AsyncMock()
    placed, fake = await _dispatch(AsyncMock(side_effect=[_res([_job()]), _res([])]), valid=False, dial=dial)
    assert placed == 0
    dial.assert_not_called()
    assert fake.table.return_value.update.call_args[0][0] == {"status": "skipped", "last_error": "appointment_changed"}


@pytest.mark.asyncio
async def test_unreadable_appointment_waits_a_minute_instead_of_guessing():
    dial = AsyncMock()
    placed, fake = await _dispatch(AsyncMock(side_effect=[_res([_job()]), _res([])]), valid=None, dial=dial)
    dial.assert_not_called()
    assert set(fake.table.return_value.update.call_args[0][0]) == {"next_attempt_at"}


@pytest.mark.asyncio
async def test_still_valid_checks_status_date_and_time():
    job = _job()
    for row, want in (({"status": "confirmed", "appointment_date": "2026-10-06", "appointment_time": "17:30:00"}, True),
                      ({"status": "cancelled", "appointment_date": "2026-10-06", "appointment_time": "17:30:00"}, False),
                      ({"status": "confirmed", "appointment_date": "2026-10-06", "appointment_time": "18:00:00"}, False)):
        with patch.object(outbound, "sb", new=AsyncMock(return_value=_res([row]))), \
             patch.object(outbound, "supabase", new=MagicMock()):
            assert await outbound._reminder_still_valid("c1", job) is want
    with patch.object(outbound, "sb", new=AsyncMock(side_effect=RuntimeError("db down"))), \
         patch.object(outbound, "supabase", new=MagicMock()):
        assert await outbound._reminder_still_valid("c1", job) is None


@pytest.mark.asyncio
async def test_unanswered_reminder_retries_in_10_minutes_only_while_in_time():
    for call_by_in, want in ((timedelta(minutes=45), "queued"), (timedelta(minutes=8), "failed")):
        job = _job(call_by_in=call_by_in, attempts=1)
        with patch.object(outbound, "get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
             patch.object(outbound, "sb", new=AsyncMock(side_effect=[_res([job]), _res([])])), \
             patch.object(outbound, "supabase", new=MagicMock()) as fake:
            await outbound.job_finished({"outbound_job_id": "j1", "clinic_id": "c1"}, success=False, error="no-answer")
        fields = fake.table.return_value.update.call_args[0][0]
        assert fields["status"] == want
        if want == "queued":
            nxt = datetime.fromisoformat(fields["next_attempt_at"])
            assert timedelta(minutes=9) < nxt - datetime.now(IST) <= timedelta(minutes=10)


@pytest.mark.asyncio
async def test_lead_retry_is_unchanged():
    job = {"attempts": 1, "purpose": "lead_followup", "context": {}}
    with patch.object(outbound, "get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch.object(outbound, "sb", new=AsyncMock(side_effect=[_res([job]), _res([])])), \
         patch.object(outbound, "supabase", new=MagicMock()) as fake:
        await outbound.job_finished({"outbound_job_id": "j1", "clinic_id": "c1"}, success=False, error="busy")
    fields = fake.table.return_value.update.call_args[0][0]
    assert fields["status"] == "queued"
    assert datetime.fromisoformat(fields["next_attempt_at"]) - datetime.now(IST) > timedelta(minutes=110)


# ---- the sweep ----

async def _sweep(sb_results, eligible=None, budget=False, allowed=True):
    sb = AsyncMock(side_effect=sb_results)
    with patch.object(outbound, "get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch("app.services.subscription.automated_outbound_allowed", return_value=allowed), \
         patch.object(outbound, "_eligible", new=AsyncMock(return_value=eligible)), \
         patch.object(outbound.store, "budget_exceeded", new=AsyncMock(return_value=budget)), \
         patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()) as fake:
        n = await outbound._queue_clinic_reminders("c1", [60], NOW.replace(hour=16, minute=35))
    return n, sb, fake


@pytest.mark.asyncio
async def test_sweep_queues_one_job_with_the_key_call_by_and_appointment():
    n, _, fake = await _sweep([_res([APPT]), _res([]), _res([{"id": "j1"}])])
    assert n == 1
    row = fake.table.return_value.insert.call_args[0][0]
    assert row["purpose"] == "appointment_reminder" and row["clinic_id"] == "c1"
    assert row["context"]["reminder_key"] == "a1|2026-10-06T17:30|60"
    assert row["context"]["call_by"] == "2026-10-06T17:20:00+05:30"
    assert row["context"]["appointment"]["time"] == "17:30" and "status" not in row


@pytest.mark.asyncio
async def test_sweep_never_queues_the_same_reminder_twice():
    existing = {"id": "j0", "status": "completed", "context": {"appointment_id": "a1",
                                                                "reminder_key": "a1|2026-10-06T17:30|60"}}
    n, sb, fake = await _sweep([_res([APPT]), _res([existing])])
    assert n == 0 and sb.await_count == 2
    fake.table.return_value.insert.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_records_an_opt_out_once_and_does_not_call():
    n, _, fake = await _sweep([_res([APPT]), _res([]), _res([{"id": "j1"}])], eligible="do_not_contact")
    row = fake.table.return_value.insert.call_args[0][0]
    assert n == 0 and row["status"] == "skipped" and row["last_error"] == "do_not_contact"


@pytest.mark.asyncio
async def test_sweep_skips_suspended_clinics_and_exhausted_budgets():
    n, sb, _ = await _sweep([], allowed=False)
    assert n == 0 and sb.await_count == 0
    n, _, fake = await _sweep([_res([APPT]), _res([])], budget=True)
    assert n == 0
    fake.table.return_value.insert.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_treats_a_concurrent_duplicate_as_done():
    n, _, _ = await _sweep([_res([APPT]), _res([]),
                            Exception('duplicate key value violates unique constraint "uq_voice_reminder_once" 23505')])
    assert n == 0


@pytest.mark.asyncio
async def test_autoqueue_only_runs_for_clinics_that_switched_it_on():
    on = {"id": "c1", "features": {"ai_receptionist": True}, "account_type": "tenant",
          "config": {"voice": {"reminders": {"enabled": True, "minutes_before": [60]}}}}
    off = {**on, "id": "c2", "config": {}}
    shadow = {**on, "id": "c3", "config": {"voice": {**on["config"]["voice"], "mode": "shadow"}}}
    no_ai = {**on, "id": "c4", "features": {}}
    q = AsyncMock(side_effect=[2, RuntimeError("boom")])
    with patch.object(outbound, "sb", new=AsyncMock(return_value=_res([on, off, shadow, no_ai, {**on, "id": "c5"}]))), \
         patch.object(outbound, "supabase", new=MagicMock()), \
         patch.object(outbound, "_queue_clinic_reminders", new=q):
        assert await outbound.autoqueue_reminders(NOW) == 2          # c5's failure does not stop the sweep
    assert [c.args[0] for c in q.await_args_list] == ["c1", "c5"]


# ---- settings ----

from tests.voice.test_voice_routes import ADMIN, CID, client  # noqa: E402

BASE = {"primary_language": "te-IN", "assistant_name": "Kriya", "pace": 1.0,
        "reception_hours": {"start": "09:00", "end": "20:00", "days": "Mon,Tue"}, "outbound": {}}


def _put(c, body, stored=None):
    with captured_config_writes() as writes, \
         patch("app.routers.voice_admin.sb", new=AsyncMock(return_value=MagicMock(data=[{"config": stored or {}}]))), \
         patch("app.routers.voice_admin.invalidate_tenant_cache"), \
         patch("app.routers.voice_admin.log_admin_action", new=AsyncMock()):
        r = c.put(f"/admin/voice/settings?clinic_id={CID}", json=body)
    return r, writes


def test_settings_validate_reminder_times():
    c, p = client(ADMIN)
    try:
        for rem in ({"enabled": True, "minutes_before": [10]}, {"enabled": True, "minutes_before": [2000]},
                    {"enabled": True, "minutes_before": []}, {"enabled": True, "minutes_before": [15, 30, 60, 120]}):
            assert _put(c, {**BASE, "reminders": rem})[0].status_code == 422, rem
        r, writes = _put(c, {**BASE, "reminders": {"enabled": True, "minutes_before": [30, 1440, 30]}})
        assert r.status_code == 200
        assert writes[-1]["config"]["voice"]["reminders"] == {"enabled": True, "minutes_before": [1440, 30]}
    finally:
        p.stop()


def test_a_client_that_does_not_send_reminders_keeps_them():
    c, p = client(ADMIN)
    stored = {"voice": {"reminders": {"enabled": True, "minutes_before": [120]}}}
    try:
        r, writes = _put(c, BASE, stored=stored)
        assert r.status_code == 200
        assert writes[-1]["config"]["voice"]["reminders"] == {"enabled": True, "minutes_before": [120]}
    finally:
        p.stop()
