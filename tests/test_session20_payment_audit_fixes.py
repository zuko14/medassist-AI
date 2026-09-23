"""Session 20 production audit: regression tests for the payment/slot fixes.

1. A late-payment refund that FAILS must not be recorded or announced as refunded.
2. A payment on a booking cancelled while unpaid is auto-refunded like an expired hold.
3. A stray payment on an already-settled booking alerts the admin.
4. The "refund needs a human" message quotes the clinic's number, not the platform's.
5. A pending_review booking holds its slot in the picker, as it does in the DB index.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from app.services.payment import PaymentService


def _webhook(payment_id: str, booking_id: str, amount: int = 50000) -> bytes:
    return json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": payment_id, "amount": amount, "notes": {"booking_id": booking_id},
        }}},
    }).encode()


class _Q:
    def __init__(self, data):
        self.data = data

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return MagicMock(data=self.data)


class _Table:
    """Booking lookup returns `booking`; the confirmed-idempotency probe returns nothing."""

    def __init__(self, booking, update_data):
        self.booking, self.update_data, self.updates = booking, update_data, []

    def select(self, fields="*"):
        return _Q([] if fields == "id" else [self.booking])

    def update(self, payload):
        self.updates.append(payload)
        return _Q(self.update_data)


async def _run(booking, refund_result, update_data=None):
    service = PaymentService()
    table = _Table(booking, [booking] if update_data is None else update_data)
    with patch("app.services.payment.supabase.table", return_value=table), \
         patch.object(service, "verify_webhook_signature", return_value=True), \
         patch.object(service, "_log_payment_event", new_callable=AsyncMock), \
         patch.object(service, "_refund_payment_id", new_callable=AsyncMock, return_value=refund_result) as refund, \
         patch.object(service, "_notify_late_payment_refunded", new_callable=AsyncMock) as told_refunded, \
         patch.object(service, "_notify_late_payment_refund_failed", new_callable=AsyncMock) as told_failed, \
         patch.object(service, "_alert_admin", new_callable=AsyncMock) as alert, \
         patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value={"id": "c-1"}):
        result = await service.process_payment_webhook(
            _webhook("pay_new", booking["id"]), "sig", webhook_secret="s", clinic_id="c-1"
        )
    return result, table, refund, told_refunded, told_failed, alert


def _booking(status, payment_id=None):
    return {
        "id": "b-1", "clinic_id": "c-1", "booking_ref": "MC-TEST", "status": status,
        "amount_paise": 50000, "patient_phone": "+919876543210", "payment_id": payment_id,
    }


@pytest.mark.asyncio
async def test_failed_late_refund_is_not_recorded_or_announced_as_refunded():
    result, table, _, told_refunded, told_failed, alert = await _run(
        _booking("expired"), {"success": False, "error": "gateway down"}
    )
    assert result["reason"] == "late_payment_refund_failed"
    assert table.updates == []  # row not flipped to 'refunded'
    told_refunded.assert_not_called()
    told_failed.assert_awaited_once()
    assert "REFUND FAILED" in alert.await_args.args[1]


@pytest.mark.asyncio
async def test_payment_on_booking_cancelled_while_unpaid_is_auto_refunded():
    result, table, refund, told_refunded, _, _ = await _run(
        _booking("cancelled"), {"success": True, "refund_id": "rfnd_1"}
    )
    assert result["action"] == "late_payment_refunded"
    refund.assert_awaited_once()
    assert table.updates[0]["status"] == "refunded"
    told_refunded.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_late_delivery_does_not_message_the_patient_twice():
    # The other event (payment_link.paid vs payment.captured) already moved the row.
    _, _, _, told_refunded, _, _ = await _run(
        _booking("expired"), {"success": True, "refund_id": "rfnd_1"}, update_data=[]
    )
    told_refunded.assert_not_called()


@pytest.mark.asyncio
async def test_stray_payment_on_settled_booking_alerts_admin():
    result, table, refund, _, _, alert = await _run(
        _booking("refunded", payment_id="pay_original"), {"success": True}
    )
    assert result["reason"] == "terminal_state_refunded"
    refund.assert_not_called()
    assert table.updates == []
    assert "pay_new" in alert.await_args.args[0]


@pytest.mark.asyncio
async def test_redelivery_of_the_settling_payment_stays_silent():
    _, _, _, _, _, alert = await _run(
        _booking("refunded", payment_id="pay_new"), {"success": True}
    )
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_manual_review_message_quotes_the_clinics_own_number():
    service = PaymentService()
    clinic = {"id": "c-1", "whatsapp_number": "+911111111111", "config": {}}
    booking = _booking("confirmed", payment_id="pay_1")
    with patch("app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock) as send, \
         patch.object(service, "resolve_patient_language", new_callable=AsyncMock, return_value="en"):
        await service.notify_cancellation_outcome(
            booking, {"success": False, "is_late": False, "reason": "razorpay_error"}, clinic=clinic
        )
    assert "+911111111111" in send.await_args.args[2]


@pytest.mark.asyncio
async def test_pending_review_booking_holds_its_slot():
    doc = {"id": "d-1", "name": "Dr. A", "available_days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
           "morning_slots": ["09:00", "09:30"], "evening_slots": []}
    appts = [{"appointment_time": "09:00:00", "status": "pending_review"}]

    def table(name):
        t = MagicMock()
        for m in ("select", "eq", "in_", "order", "limit"):
            setattr(t, m, MagicMock(return_value=t))
        t.execute = MagicMock(return_value=MagicMock(
            data=appts if name == "appointments" else [doc] if name == "doctors" else []))
        return t

    # test_payment.py swaps a mock into sys.modules["app.database"] at import.
    import importlib
    import sys

    if not hasattr(sys.modules.get("app.database"), "__file__"):
        sys.modules.pop("app.database", None)
    app_db = importlib.import_module("app.database")

    sb_mock = MagicMock()
    sb_mock.table.side_effect = table
    app_db._doctor_cache.clear()
    app_db._holiday_cache.clear()
    with patch.object(app_db, "supabase", sb_mock):
        slots, err = await app_db.get_available_slots("c-1", "Dr. A", "2035-05-15")
    assert err is None
    assert slots == ["09:30"]


# ── Cancellation window: enforced for the patient, never for staff ─────────
from datetime import datetime, timedelta, timezone  # noqa: E402


def test_slot_parser_accepts_the_postgres_time_format():
    p = PaymentService()
    assert p._parse_slot_datetime("2026-09-23", "10:30:00") == p._parse_slot_datetime("2026-09-23", "10:30")
    assert p._parse_slot_datetime("2026-09-23", "10:30:00") is not None
    assert p._parse_slot_datetime("2026-09-23", None) is None  # lab test: no slot time


async def _refund_in_one_hour(enforce_window):
    """A paid booking whose slot (stored as Postgres returns it) is 1h away, 4h window."""
    ist = timezone(timedelta(hours=5, minutes=30))
    slot = datetime.now(ist) + timedelta(hours=1)
    booking = {
        "id": "b-1", "clinic_id": "c-1", "status": "confirmed", "payment_id": "pay_1",
        "amount_paise": 50000, "appointment_date": slot.strftime("%Y-%m-%d"),
        "appointment_time": slot.strftime("%H:%M:00"),
    }
    service = PaymentService()
    with patch("app.services.payment.supabase.table", return_value=_Table(booking, [booking])), \
         patch.object(service, "_log_payment_event", new_callable=AsyncMock), \
         patch.object(service, "_create_razorpay_refund", new_callable=AsyncMock,
                      return_value={"id": "rfnd_1"}) as gateway:
        kwargs = {} if enforce_window is None else {"enforce_window": enforce_window}
        result = await service.initiate_refund(
            "b-1", clinic={"id": "c-1", "config": {"cancellation_window_hours": 4}}, **kwargs
        )
    return result, gateway


@pytest.mark.asyncio
async def test_patient_cancel_inside_the_window_is_not_refunded():
    if datetime.now(timezone(timedelta(hours=5, minutes=30))).hour >= 23:
        pytest.skip("slot would roll past midnight")
    result, gateway = await _refund_in_one_hour(None)
    assert result["success"] is False and result["is_late"] is True
    gateway.assert_not_called()


@pytest.mark.asyncio
async def test_admin_refund_ignores_the_patient_window():
    if datetime.now(timezone(timedelta(hours=5, minutes=30))).hour >= 23:
        pytest.skip("slot would roll past midnight")
    result, gateway = await _refund_in_one_hour(False)
    assert result["success"] is True
    gateway.assert_awaited_once()


# ── Doctor leave: a clinic-side cancellation must refund a paid booking ────
from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _always_locked(*_a, **_k):
    yield True


async def _run_leave_job(appt, refund_result):
    from app.services import scheduler as sched_mod
    from app.services.payment import payment_service

    leave = {"clinic_id": "c-1", "doctor_name": "Dr. A", "leave_date": "2035-05-15"}
    updates = []

    class _T:
        def __init__(self, name):
            self.name = name

        def select(self, *_):
            return _Q([leave] if self.name == "doctor_leaves" else [appt])

        def update(self, payload):
            updates.append(payload)
            return _Q([appt])

    sb_mock = MagicMock()
    sb_mock.table.side_effect = _T
    with patch.object(sched_mod, "supabase", sb_mock), \
         patch("app.services.distributed_lock.distributed_job_lock", _always_locked), \
         patch.object(sched_mod, "get_clinic_by_id", new_callable=AsyncMock, return_value={"id": "c-1"}), \
         patch.object(sched_mod.whatsapp_service, "send_template", new_callable=AsyncMock) as template, \
         patch.object(payment_service, "initiate_refund", new_callable=AsyncMock, return_value=refund_result) as refund, \
         patch.object(payment_service, "notify_cancellation_outcome", new_callable=AsyncMock) as outcome, \
         patch.object(payment_service, "_alert_admin", new_callable=AsyncMock) as alert:
        await sched_mod.SchedulerService().check_doctor_leaves()
    return updates, template, refund, outcome, alert


def _leave_appt(payment_id):
    return {"id": "a-1", "clinic_id": "c-1", "doctor_name": "Dr. A", "booking_ref": "MC-1",
            "appointment_date": "2035-05-15", "patient_phone": "+919876543210",
            "status": "confirmed", "payment_id": payment_id}


@pytest.mark.asyncio
async def test_doctor_leave_refunds_a_paid_booking_ignoring_the_patient_cutoff():
    updates, template, refund, outcome, alert = await _run_leave_job(
        _leave_appt("pay_1"), {"success": True, "refund_id": "rfnd_1"}
    )
    assert refund.await_args.kwargs["enforce_window"] is False
    assert updates == []  # initiate_refund already moved the row to 'refunded'
    template.assert_awaited_once()
    outcome.assert_awaited_once()
    alert.assert_not_called()


@pytest.mark.asyncio
async def test_doctor_leave_refund_failure_still_cancels_and_alerts():
    updates, _, _, outcome, alert = await _run_leave_job(
        _leave_appt("pay_1"), {"success": False, "reason": "razorpay_error"}
    )
    assert updates == [{"status": "cancelled"}]
    alert.assert_awaited_once()
    outcome.assert_awaited_once()  # patient told a human will refund


@pytest.mark.asyncio
async def test_doctor_leave_unpaid_booking_behaves_as_before():
    updates, template, refund, outcome, _ = await _run_leave_job(_leave_appt(None), None)
    refund.assert_not_called()
    outcome.assert_not_called()
    assert updates == [{"status": "cancelled"}]
    template.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_confirm_loses_to_a_concurrent_reject():
    """Reject refunded the booking between Confirm's read and write: no confirm, no message."""
    service = PaymentService()
    table = _Table(_booking("pending_review", payment_id="pay_1"), update_data=[])
    with patch("app.services.payment.supabase.table", return_value=table), \
         patch.object(service, "_notify_payment_confirmed", new_callable=AsyncMock) as told:
        result = await service.admin_confirm_booking("b-1", clinic_id="c-1")
    assert result == {"success": False, "reason": "booking_changed_concurrently"}
    told.assert_not_called()


# ── Check-in: a stale panel must not hand a cancelled booking a queue token ─
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "refunded", "expired", "pending_payment"])
async def test_check_in_refuses_a_non_bookable_status(status):
    import importlib
    import sys

    if not hasattr(sys.modules.get("app.database"), "__file__"):
        sys.modules.pop("app.database", None)
    app_db = importlib.import_module("app.database")

    row = {"id": "a-1", "clinic_id": "c-1", "doctor_name": "Dr. A", "branch_id": None,
           "appointment_date": "2035-05-15", "token_number": None, "status": status}
    sb_mock = MagicMock()
    sb_mock.table.return_value = _Q([row])
    with patch.object(app_db, "supabase", sb_mock):
        with pytest.raises(ValueError, match=status):
            await app_db.check_in_appointment("c-1", "a-1")
