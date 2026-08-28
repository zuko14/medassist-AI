"""Tests for the fast-cycle payment status poller — verifies near-instant confirmation."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ──────────────────────────────────────────────────────────────────

CLINIC_ID = "clinic-1"
PHONE = "919876543210"

def _make_pending_booking(booking_id, payment_link_id, created_minutes_ago=2):
    """Create a mock pending_payment booking dictionary."""
    created = (
        datetime.now(timezone.utc) - timedelta(minutes=created_minutes_ago)
    ).isoformat()
    return {
        "id": booking_id,
        "clinic_id": CLINIC_ID,
        "booking_ref": f"KA-{booking_id[:6]}",
        "patient_phone": PHONE,
        "razorpay_payment_link_id": payment_link_id,
        "amount_paise": 12500,
        "doctor_name": "Dr. Meena Patel",
        "department": "General",
        "appointment_date": "2026-08-28",
        "appointment_time": "17:00",
        "patient_name": "Test Patient",
        "branch_id": None,
        "status": "pending_payment",
        "created_at": created,
    }


# ── Test 1: Paid booking is confirmed instantly ─────────────────────────────


@pytest.mark.asyncio
async def test_fast_poll_confirms_paid_booking():
    """When Razorpay shows 'paid', the booking should be confirmed immediately
    and the patient should receive a WhatsApp notification."""
    from app.services.payment import PaymentService

    svc = PaymentService()

    booking = _make_pending_booking("bk-001", "plink_abc123")

    mock_supabase = MagicMock()

    # Query returns one recent pending booking
    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.gte.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.return_value = MagicMock(data=[booking])
    mock_supabase.table.return_value.select.return_value = mock_select

    # CAS update returns the confirmed booking
    mock_update = MagicMock()
    mock_update.update.return_value = mock_update
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=[{**booking, "status": "confirmed"}])
    mock_supabase.table.return_value.update.return_value = mock_update

    mock_clinic = {"id": CLINIC_ID, "name": "Test Clinic", "razorpay_key_id": "rzp_test", "razorpay_key_secret": "secret"}

    with patch("app.services.payment.supabase", mock_supabase), \
         patch.object(svc, "_check_payment_link_status", new_callable=AsyncMock) as mock_check, \
         patch.object(svc, "_notify_payment_confirmed", new_callable=AsyncMock) as mock_notify, \
         patch.object(svc, "_increment_patient_visit_count", new_callable=AsyncMock), \
         patch.object(svc, "_log_payment_event"), \
         patch("app.services.payment.get_razorpay_creds", return_value=("rzp_test", "secret", "whsec")), \
         patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value=mock_clinic):

        mock_check.return_value = {"status": "paid", "payment_id": "pay_xyz789"}

        count = await svc.poll_recent_pending_payments()

    assert count == 1
    mock_check.assert_called_once_with("plink_abc123", key_id="rzp_test", key_secret="secret")
    mock_notify.assert_called_once()
    notified_booking = mock_notify.call_args[0][0]
    assert notified_booking["status"] == "confirmed"
    assert notified_booking["payment_id"] == "pay_xyz789"


# ── Test 2: Unpaid booking is not touched ───────────────────────────────────


@pytest.mark.asyncio
async def test_fast_poll_skips_unpaid_booking():
    """When Razorpay shows 'created' (not yet paid), the booking should remain
    in pending_payment and no notification should be sent."""
    from app.services.payment import PaymentService

    svc = PaymentService()

    booking = _make_pending_booking("bk-002", "plink_notpaid")

    mock_supabase = MagicMock()
    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.gte.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.return_value = MagicMock(data=[booking])
    mock_supabase.table.return_value.select.return_value = mock_select

    mock_clinic = {"id": CLINIC_ID, "name": "Test Clinic"}

    with patch("app.services.payment.supabase", mock_supabase), \
         patch.object(svc, "_check_payment_link_status", new_callable=AsyncMock) as mock_check, \
         patch.object(svc, "_notify_payment_confirmed", new_callable=AsyncMock) as mock_notify, \
         patch("app.services.payment.get_razorpay_creds", return_value=("rzp_test", "secret", "whsec")), \
         patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value=mock_clinic):

        mock_check.return_value = {"status": "created", "payment_id": ""}

        count = await svc.poll_recent_pending_payments()

    assert count == 0
    mock_notify.assert_not_called()


# ── Test 3: Already-confirmed booking is idempotent ─────────────────────────


@pytest.mark.asyncio
async def test_fast_poll_idempotent_if_already_confirmed():
    """If the CAS update returns 0 rows (webhook already confirmed), no duplicate
    notification should be sent."""
    from app.services.payment import PaymentService

    svc = PaymentService()

    booking = _make_pending_booking("bk-003", "plink_already")

    mock_supabase = MagicMock()
    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.gte.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.return_value = MagicMock(data=[booking])
    mock_supabase.table.return_value.select.return_value = mock_select

    # CAS update returns EMPTY (already confirmed by webhook)
    mock_update = MagicMock()
    mock_update.update.return_value = mock_update
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=[])
    mock_supabase.table.return_value.update.return_value = mock_update

    mock_clinic = {"id": CLINIC_ID, "name": "Test Clinic"}

    with patch("app.services.payment.supabase", mock_supabase), \
         patch.object(svc, "_check_payment_link_status", new_callable=AsyncMock) as mock_check, \
         patch.object(svc, "_notify_payment_confirmed", new_callable=AsyncMock) as mock_notify, \
         patch("app.services.payment.get_razorpay_creds", return_value=("rzp_test", "secret", "whsec")), \
         patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value=mock_clinic):

        mock_check.return_value = {"status": "paid", "payment_id": "pay_dup"}

        count = await svc.poll_recent_pending_payments()

    assert count == 0
    mock_notify.assert_not_called()


# ── Test 4: No pending bookings returns zero ────────────────────────────────


@pytest.mark.asyncio
async def test_fast_poll_no_recent_bookings():
    """When there are no recent pending_payment bookings, the poller should
    return 0 without making any Razorpay API calls."""
    from app.services.payment import PaymentService

    svc = PaymentService()

    mock_supabase = MagicMock()
    mock_select = MagicMock()
    mock_select.eq.return_value = mock_select
    mock_select.gte.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.return_value = MagicMock(data=[])
    mock_supabase.table.return_value.select.return_value = mock_select

    with patch("app.services.payment.supabase", mock_supabase), \
         patch.object(svc, "_check_payment_link_status", new_callable=AsyncMock) as mock_check:

        count = await svc.poll_recent_pending_payments()

    assert count == 0
    mock_check.assert_not_called()
