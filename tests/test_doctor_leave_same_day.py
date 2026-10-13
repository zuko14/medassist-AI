"""Doctor leave cancellation for near dates (Kriya Ketu bug register D-01, D-06, D-17).

D-01  check_doctor_leaves ran once a day at 08:00 over leave dates from
      TOMORROW to +7. A leave for today, or one entered after 08:00 for
      tomorrow, was never processed: its bookings stayed confirmed and the
      patients came in to an absent doctor.
D-06  It computed "tomorrow" from datetime.now() (container time, UTC on Render)
      instead of the clinic's IST clock.
D-17  LeaveCreate accepted any leave_type, but slot blocking and the job only
      understand full / half_morning / half_evening: a leave stored as e.g.
      "full_day" blocked nothing and cancelled nothing.
"""

from contextlib import asynccontextmanager
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.services import scheduler as sched_mod
from app.services.scheduler import CLINIC_TZ

TODAY = date(2035, 5, 15)
NOW_11AM = datetime(2035, 5, 15, 11, 0, tzinfo=CLINIC_TZ)


class _Rec:
    """A PostgREST-builder fake that records filter calls and returns fixed rows."""

    def __init__(self, rows, calls):
        self.rows, self.calls = rows, calls

    def __getattr__(self, name):
        def _f(*a, **k):
            self.calls.append((name, a))
            return self
        return _f

    def execute(self):
        return MagicMock(data=self.rows)


def _fake_db(leaves, appts, calls, updates):
    class _T:
        def __init__(self, name):
            self.name = name

        def select(self, *_):
            return _Rec(leaves if self.name == "doctor_leaves" else appts, calls.setdefault(self.name, []))

        def update(self, payload):
            updates.append(payload)
            return _Rec([{}], [])

    db = MagicMock()
    db.table.side_effect = _T
    return db


def _locked(results):
    """distributed_job_lock fake yielding the given acquire results in order."""
    seq = iter(results)

    @asynccontextmanager
    async def _lock(*_a, **_k):
        yield next(seq)

    return _lock


def _appt(id_, time_):
    return {"id": id_, "clinic_id": "c-1", "doctor_name": "Dr. A", "booking_ref": id_,
            "appointment_date": "2035-05-15", "appointment_time": time_,
            "patient_phone": "+919876543210", "status": "confirmed", "payment_id": None}


def _patches(db, lock_results=(True,)):
    return (
        patch.object(sched_mod, "supabase", db),
        patch("app.services.distributed_lock.distributed_job_lock", _locked(lock_results)),
        patch.object(sched_mod, "get_clinic_by_id", new_callable=AsyncMock, return_value={"id": "c-1"}),
        patch.object(sched_mod.whatsapp_service, "send_template", new_callable=AsyncMock),
        patch.object(sched_mod, "_today_local", return_value=TODAY),
        patch.object(sched_mod, "_now_local", return_value=NOW_11AM),
    )


async def _run(coro_factory, db, lock_results=(True,)):
    ps = _patches(db, lock_results)
    for p in ps:
        p.start()
    try:
        await coro_factory()
    finally:
        for p in reversed(ps):
            p.stop()


@pytest.mark.asyncio
async def test_daily_job_window_starts_today_on_the_ist_clock():
    calls, updates = {}, []
    db = _fake_db([], [], calls, updates)
    await _run(lambda: sched_mod.SchedulerService().check_doctor_leaves(), db)
    filters = calls["doctor_leaves"]
    assert ("gte", ("leave_date", "2035-05-15")) in filters
    assert ("lte", ("leave_date", "2035-05-22")) in filters


@pytest.mark.asyncio
async def test_same_day_leave_cancels_only_bookings_still_ahead():
    leave = {"clinic_id": "c-1", "doctor_name": "Dr. A", "leave_date": "2035-05-15", "leave_type": "full"}
    calls, updates = {}, []
    db = _fake_db([leave], [_appt("past", "09:30:00"), _appt("ahead", "14:00:00")], calls, updates)
    await _run(lambda: sched_mod.SchedulerService().check_doctor_leaves(), db)
    assert updates == [{"status": "cancelled"}]  # only the 14:00 booking


@pytest.mark.asyncio
async def test_new_near_leave_is_processed_immediately():
    leave = {"clinic_id": "c-1", "doctor_name": "Dr. A", "leave_date": "2035-05-16", "leave_type": "full"}
    appt = dict(_appt("tomorrow", "10:00:00"), appointment_date="2035-05-16")
    calls, updates = {}, []
    db = _fake_db([], [appt], calls, updates)
    await _run(lambda: sched_mod.SchedulerService().process_new_leaves([leave], wait_seconds=0), db)
    assert updates == [{"status": "cancelled"}]
    assert "doctor_leaves" not in calls  # works on the rows it was given


@pytest.mark.asyncio
async def test_new_near_leave_waits_for_a_running_sweep_then_processes_once():
    leave = {"clinic_id": "c-1", "doctor_name": "Dr. A", "leave_date": "2035-05-16", "leave_type": "full"}
    appt = dict(_appt("tomorrow", "10:00:00"), appointment_date="2035-05-16")
    calls, updates = {}, []
    db = _fake_db([], [appt], calls, updates)
    await _run(lambda: sched_mod.SchedulerService().process_new_leaves([leave], wait_seconds=0), db,
               lock_results=(False, False, True))
    assert updates == [{"status": "cancelled"}]


def _create_leave_patches(spawn):
    db = MagicMock()
    db.table.return_value.insert.side_effect = lambda rows: _Rec(rows, [])
    return (
        patch("app.routers.admin.supabase", db),
        patch("app.routers.admin.log_admin_action", new_callable=AsyncMock),
        patch("app.routers.admin.spawn_background_task", spawn),
        patch("app.utils.helpers.today_ist", return_value=TODAY),
    )


async def _create(leave_date, end_date=None):
    from app.routers.admin import AdminUser, LeaveCreate, create_leave

    spawn = MagicMock(side_effect=lambda coro, **_k: coro.close())
    ps = _create_leave_patches(spawn)
    for p in ps:
        p.start()
    try:
        await create_leave(
            leave=LeaveCreate(doctor_name="Dr. A", leave_date=leave_date, end_date=end_date, leave_type="full"),
            request=None, clinic_id="c-1",
            user=AdminUser("admin", role="clinic_admin", clinic_id="c-1", user_id="u-1"),
        )
    finally:
        for p in reversed(ps):
            p.stop()
    return spawn


@pytest.mark.asyncio
async def test_create_leave_for_tomorrow_triggers_immediate_cancellation():
    spawn = await _create(date(2035, 5, 16))
    spawn.assert_called_once()


@pytest.mark.asyncio
async def test_create_leave_next_week_leaves_it_to_the_daily_sweep():
    spawn = await _create(date(2035, 5, 20))
    spawn.assert_not_called()


@pytest.mark.parametrize("bad", ["full_day", "FULL", "half", ""])
def test_leave_type_outside_the_three_known_values_is_rejected(bad):
    from app.routers.admin import LeaveCreate

    with pytest.raises(ValidationError):
        LeaveCreate(doctor_name="Dr. A", leave_date=TODAY, leave_type=bad)
