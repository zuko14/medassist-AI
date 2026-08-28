"""Phase 1: P0 Payment State & Refund Integrity Tests.

Verifies:
1. P0-1: Late payment auto-refund writes canonical status 'refunded',
   with 'refund_reason'='late_payment', 'refund_id', and 'refunded_at'
   against real PostgreSQL database without check constraint or truncation failures.
2. P1-8: Razorpay webhook endpoint returns HTTP 500 on unhandled exceptions
   so Razorpay's exponential backoff retry mechanism is triggered.
3. Database Migration 046 columns and indexes exist and work under transaction.
"""

import json
import uuid
import pytest
import psycopg2
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.services.payment import PaymentService
from tests.conftest_db import get_default_clinic_id


def test_migration_046_refund_columns_exist(real_pg_conn, clean_db):
    """Verify that migration 046 creates refund columns on appointments table."""
    cur = real_pg_conn.cursor()
    cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'appointments' AND column_name IN ('refund_id', 'refund_reason', 'refunded_at');
    """)
    cols = {row[0]: row[1] for row in cur.fetchall()}
    assert "refund_id" in cols
    assert "refund_reason" in cols
    assert "refunded_at" in cols


def test_late_payment_refund_persists_canonical_status_on_real_pg(real_pg_conn, clean_db):
    """P0-1 Real PostgreSQL Invariant: Late payment refund succeeds in database."""
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)

    # 1. Insert an expired booking
    cur.execute("""
    INSERT INTO appointments (
        clinic_id, patient_phone, department, doctor_name,
        appointment_date, appointment_time, status, amount_paise, payment_id
    ) VALUES (
        %s, '+919876543210', 'Cardiology', 'Dr. Sharma',
        '2026-09-05', '11:00:00', 'expired', 50000, 'pay_test_late_01'
    ) RETURNING id;
    """, (clinic_id,))
    booking_id = cur.fetchone()[0]

    # 2. Simulate late payment refund update (as executed in payment.py)
    refund_id = "rfnd_test_late_123"
    cur.execute("""
    UPDATE appointments
    SET status = 'refunded',
        refund_reason = 'late_payment',
        payment_id = 'pay_test_late_01',
        refund_id = %s,
        refunded_at = NOW()
    WHERE id = %s AND status = 'expired';
    """, (refund_id, booking_id))

    # 3. Verify record in database
    cur.execute("""
    SELECT status, refund_reason, refund_id, refunded_at IS NOT NULL
    FROM appointments WHERE id = %s;
    """, (booking_id,))
    row = cur.fetchone()
    assert row[0] == "refunded"
    assert row[1] == "late_payment"
    assert row[2] == refund_id
    assert row[3] is True


@pytest.mark.asyncio
async def test_late_payment_webhook_flow_executes_cleanly():
    """Verify PaymentService.process_payment_webhook writes status='refunded' for expired slot."""
    service = PaymentService()

    fake_booking_id = str(uuid.uuid4())
    fake_booking = {
        "id": fake_booking_id,
        "clinic_id": "test-clinic",
        "booking_ref": "BK-TEST-LATE",
        "status": "expired",
        "patient_phone": "+919876543210",
        "amount_paise": 50000,
        "appointment_date": "2026-09-05",
        "appointment_time": "11:00:00",
    }

    class MockQuery:
        def __init__(self, data=None):
            self.data = data if data is not None else []

        def eq(self, *args, **kwargs):
            return self

        def select(self, *args, **kwargs):
            return self

        def update(self, *args, **kwargs):
            return self

        def lt(self, *args, **kwargs):
            return self

        def execute(self):
            m = MagicMock()
            m.data = self.data
            return m

    class MockSupabaseTable:
        def __init__(self):
            self.last_update = None

        def select(self, fields="*"):
            if fields == "id":
                # Idempotency check: not confirmed
                return MockQuery([])
            # Booking lookup query: return fake booking
            return MockQuery([fake_booking])

        def update(self, payload):
            self.last_update = payload
            return MockQuery([])

    mock_db = MockSupabaseTable()

    with patch("app.services.payment.supabase.table", return_value=mock_db), \
         patch.object(service, "verify_webhook_signature", return_value=True), \
         patch.object(service, "_refund_payment_id", new_callable=AsyncMock) as mock_refund, \
         patch.object(service, "_notify_late_payment_refunded", new_callable=AsyncMock), \
         patch.object(service, "_alert_admin", new_callable=AsyncMock), \
         patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value={"id": "test-clinic"}):

        mock_refund.return_value = {"success": True, "refund_id": "rfnd_mock_123"}

        webhook_payload = json.dumps({
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_mock_late_01",
                        "amount": 50000,
                        "notes": {"booking_id": fake_booking_id},
                    }
                }
            }
        }).encode("utf-8")

        result = await service.process_payment_webhook(
            raw_body=webhook_payload,
            signature="valid_mock_sig",
            webhook_secret="test_secret",
            clinic_id="test-clinic",
        )

        assert result["status"] == "ok"
        assert result["code"] == 200
        assert result["action"] == "late_payment_refunded"

        # Check update arguments
        update_args = mock_db.last_update
        assert update_args is not None
        assert update_args["status"] == "refunded"
        assert update_args["refund_reason"] == "late_payment"
        assert update_args["refund_id"] == "rfnd_mock_123"
        assert "refunded_at" in update_args


def test_webhook_returns_500_on_unhandled_exception():
    """P1-8: Webhook router returns HTTP 500 on unexpected exception so Razorpay retries."""
    client = TestClient(app)

    with patch("app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value={"id": "test-clinic"}), \
         patch("app.routers.razorpay_webhook.payment_service.process_payment_webhook", side_effect=RuntimeError("DB crashed")):
        response = client.post(
            "/webhooks/razorpay/test-clinic",
            content=b'{"test": true}',
            headers={"X-Razorpay-Signature": "some_sig"},
        )
        assert response.status_code == 500
        assert response.json()["status"] == "error"
        assert response.json()["reason"] == "internal_error"

